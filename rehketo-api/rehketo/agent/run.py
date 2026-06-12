from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import select, update

from rehketo.agent.events import transform_chunk
from rehketo.agent.graph import build_agent
from rehketo.agent.prompt import assemble_system_prompt
from rehketo.agent.title import generate_title_if_needed
from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Message, Run, UserPreferences, UserRole
from rehketo.mcp.registry import build_run_toolset
from rehketo.mcp.servers import allowed_servers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)


async def _load_history(
    db: AsyncSession, conversation_id: UUID
) -> list[AIMessage | HumanMessage | SystemMessage]:
    """Load prior user/assistant turns for the agent. The system prompt is
    assembled in run_agent and passed to build_agent via
    create_deep_agent(system_prompt=...); do NOT prepend one here or the
    model sees the same prompt twice."""
    msgs = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    result: list[AIMessage | HumanMessage | SystemMessage] = []
    for m in msgs:
        text = (
            m.content if isinstance(m.content, str) else str(m.content.get("text", ""))
        )
        if m.role == "user":
            result.append(HumanMessage(content=text))
        elif m.role == "assistant":
            result.append(AIMessage(content=text))
    return result


async def run_agent(run_id: UUID, bus: RunEventBus) -> None:  # noqa: PLR0915  # orchestrator: three terminal branches in one function is the simplest correct shape
    """Drive the agent for `run_id`. Called as an asyncio.Task.

    Terminal-event discipline: the SSE handler (and the UI's `subscribeRun`)
    closes ONLY on `run.ended`. To guarantee delivery on every terminal path
    — success, failure, cancellation, *or* a failure during finalization —
    the `run.ended` publish lives in a single outer ``finally`` block,
    wrapped in ``contextlib.suppress`` so a broken bus cannot leak the
    real exception or strand the stream. Each branch handles its own
    state-transition events (``run.status=…``) and persistence; the
    terminator is the orchestrator's responsibility, not each branch's.
    """
    # Bind only what the failure/cancel branches and the terminator need
    # before the outer `try` starts, so any later error — including the
    # status flip, history load, or preferences fetch below — finalizes the
    # run instead of stranding it in 'running'. If THIS load fails there is
    # genuinely nothing to finalize.
    async with sessionmaker()() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        conversation_id: UUID = run.conversation_id
        user_id: UUID = run.user_id

    assembled_text = ""

    try:
        try:
            async with sessionmaker()() as db:
                await db.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status="running",
                        started_at=datetime.now(UTC),
                    )
                )
                await db.commit()
                await bus.publish(
                    str(run_id), {"type": "run.status", "status": "running"}
                )

                history = await _load_history(db, conversation_id)
                prefs = (
                    await db.execute(
                        select(UserPreferences).where(
                            UserPreferences.user_id == user_id
                        )
                    )
                ).scalar_one_or_none()
                custom_instructions = prefs.custom_instructions if prefs else None
                roles = (
                    (
                        await db.execute(
                            select(UserRole.role).where(UserRole.user_id == user_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                servers = await allowed_servers(db, user_id=user_id, roles=roles)
            system_prompt = assemble_system_prompt(custom_instructions)

            # MCP clients live exactly as long as the agent run; the exit
            # stack closes them on every path (success, failure, cancel).
            async with contextlib.AsyncExitStack() as stack:
                tools, interrupt_on = await build_run_toolset(
                    stack, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id), system_prompt, tools=tools, interrupt_on=interrupt_on
                ):
                    async for chunk in agent.astream(
                        {"messages": history},
                        config={"configurable": {"thread_id": str(run_id)}},
                        stream_mode="messages",
                    ):
                        for event in transform_chunk(chunk):  # type: ignore[arg-type]
                            await bus.publish(str(run_id), event)
                            if event["type"] == "message.delta":
                                assembled_text += str(event["delta"])

            # Persist the assistant message and finalize the run.
            assistant_id = uuid4()
            async with sessionmaker()() as db:
                db.add(
                    Message(
                        id=assistant_id,
                        conversation_id=conversation_id,
                        role="assistant",
                        content={"text": assembled_text},
                        run_id=run_id,
                    )
                )
                await db.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status="succeeded",
                        finished_at=datetime.now(UTC),
                    )
                )
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=datetime.now(UTC))
                )
                await db.commit()
                # Re-read the persisted message so the wire shape matches the
                # MessageOut that GET /conversations/{id} returns. The UI can
                # then replace its streaming bubble with a server-authoritative
                # object (same id, same created_at on reload).
                persisted = (
                    await db.execute(select(Message).where(Message.id == assistant_id))
                ).scalar_one()
                message_payload: dict[str, object] = {
                    "id": str(persisted.id),
                    "conversation_id": str(persisted.conversation_id),
                    "role": persisted.role,
                    "content": persisted.content,
                    "run_id": str(persisted.run_id) if persisted.run_id else None,
                    "created_at": persisted.created_at.isoformat()
                    if persisted.created_at
                    else None,
                    "run_status": "succeeded",
                    "run_error": None,
                }

            await bus.publish(
                str(run_id),
                {
                    "type": "message.complete",
                    "message": message_payload,
                },
            )

            # Emit succeeded eagerly so the UI clears its 'running' indicator as
            # soon as the reply is complete — before the title-generation window.
            # The SSE handler does NOT close on succeeded; it waits for run.ended.
            await bus.publish(
                str(run_id), {"type": "run.status", "status": "succeeded"}
            )

            # Title generation is best-effort. It already swallows its own
            # exceptions internally; the explicit try/except here is defense
            # in depth — if a regression lets one escape, it must NOT trip
            # the outer `except Exception` path and persist a phantom
            # failed assistant message on top of the succeeded one.
            try:
                new_title = await generate_title_if_needed(conversation_id)
            except Exception:
                logger.exception(
                    "title generation failed for conversation %s", conversation_id
                )
                new_title = None
            if new_title is not None:
                await bus.publish(
                    str(run_id),
                    {
                        "type": "conversation.updated",
                        "conversation_id": str(conversation_id),
                        "title": new_title,
                    },
                )

        except asyncio.CancelledError:
            # Shield the finalizer so a second cancel during cleanup doesn't strand
            # the run in 'running' status. The re-raise at the end still propagates
            # the cancellation so asyncio marks the task as cancelled. The outer
            # `finally` then publishes run.ended after the shielded work completes.
            async def _finalize_cancel() -> None:
                async with sessionmaker()() as db:
                    # Persist the partial assistant text, same rationale as the
                    # failed branch — reload shows a 'cancelled' badge via the
                    # run_status join on MessageOut.
                    db.add(
                        Message(
                            id=uuid4(),
                            conversation_id=conversation_id,
                            role="assistant",
                            content={"text": assembled_text},
                            run_id=run_id,
                        )
                    )
                    await db.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status="cancelled",
                            finished_at=datetime.now(UTC),
                        )
                    )
                    await db.execute(
                        update(Conversation)
                        .where(Conversation.id == conversation_id)
                        .values(updated_at=datetime.now(UTC))
                    )
                    await db.commit()
                await bus.publish(
                    str(run_id), {"type": "run.status", "status": "cancelled"}
                )

            await asyncio.shield(_finalize_cancel())
            raise

        except Exception as exc:
            # Broad catch is intentional: this is a top-level task handler that
            # must finalize DB state and publish a terminal event for any failure,
            # including unexpected LangGraph / LangChain internals. CancelledError
            # is NOT a subclass of Exception in Python 3.8+, so it is not caught here.
            logger.exception("run_agent failed run_id=%s", str(run_id))
            async with sessionmaker()() as db:
                # Persist whatever partial assistant text the stream produced.
                # GET /conversations/{id} joins Run.status/Run.error so the UI
                # can render this bubble with a 'failed' badge on reload without
                # replaying the SSE stream. Empty text is fine — it still marks
                # that an attempt happened.
                db.add(
                    Message(
                        id=uuid4(),
                        conversation_id=conversation_id,
                        role="assistant",
                        content={"text": assembled_text},
                        run_id=run_id,
                    )
                )
                await db.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status="failed",
                        error={"code": "llm_failure", "message": str(exc)},
                        finished_at=datetime.now(UTC),
                    )
                )
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=datetime.now(UTC))
                )
                await db.commit()
            await bus.publish(
                str(run_id),
                {
                    "type": "run.status",
                    "status": "failed",
                    "error": {"code": "llm_failure", "message": str(exc)},
                },
            )

    finally:
        # Single, guaranteed terminator. Suppress publish failures so a broken
        # bus cannot mask the real exception. If this publish fails the DB is
        # down — the run's own state writes have already failed the same way —
        # and any still-attached subscriber will be closed out by the startup
        # sweep's terminal events after the next restart.
        with contextlib.suppress(Exception):
            await bus.publish(str(run_id), {"type": "run.ended"})
