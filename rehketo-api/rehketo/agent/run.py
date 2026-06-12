from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from sqlalchemy import select, update

from rehketo.agent.approval import resolve_interrupt
from rehketo.agent.events import transform_chunk
from rehketo.agent.graph import build_agent
from rehketo.agent.prompt import assemble_system_prompt
from rehketo.agent.segments import SegmentTracker
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
        if isinstance(m.content, dict) and m.content.get("channel") == "thinking":
            # Narration is not model context — only answers feed back,
            # matching how Anthropic drops thinking between turns.
            continue
        text = (
            m.content if isinstance(m.content, str) else str(m.content.get("text", ""))
        )
        if m.role == "user":
            result.append(HumanMessage(content=text))
        elif m.role == "assistant":
            result.append(AIMessage(content=text))
    return result


def _assistant_rows(
    segments: SegmentTracker, conversation_id: UUID, run_id: UUID
) -> list[Message]:
    """One Message row per AI turn. Thinking rows carry channel='thinking'
    and their last-delta timestamp so they interleave correctly with the
    adapter-persisted tool rows; the final turn is the answer — plain {text}
    content, created_at left to the DB default. An empty run still persists
    the single empty answer row (it marks that an attempt happened).

    Clock-source note: thinking rows are stamped with ``seg.last_delta_at``
    (app-process clock) while the answer row and tool run_events use Postgres
    ``func.now()`` (DB clock). The transcript GET sorts across both, which
    works because the API and Postgres share the same host and their clocks
    are closely synced. Revisit with a per-run sequence column if hosts split.
    Two thinking rows can theoretically tie at microsecond resolution —
    accepted; the symptom is two narration blobs swapping inside the Working
    block, not data loss."""
    rows = [
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="assistant",
            content={"text": seg.text, "channel": "thinking"},
            run_id=run_id,
            created_at=seg.last_delta_at,  # app clock; see clock-source note above
        )
        for seg in segments.thinking
    ]
    rows.append(
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="assistant",
            content={"text": segments.answer_text},
            run_id=run_id,
        )
    )
    return rows


async def run_agent(run_id: UUID, bus: RunEventBus) -> None:  # noqa: C901,PLR0912,PLR0915  # orchestrator: resume loop + three terminal branches is the simplest correct shape
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

    segments = SegmentTracker()

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
                    config: Any = {"configurable": {"thread_id": str(run_id)}}
                    stream_input: Any = {"messages": history}
                    while True:
                        async for chunk in agent.astream(
                            stream_input,
                            config=config,
                            stream_mode="messages",
                        ):
                            for event in transform_chunk(chunk):  # type: ignore[arg-type]
                                await bus.publish(str(run_id), event)
                                if event["type"] == "message.delta":
                                    segments.add_delta(
                                        event.get("message_id"), str(event["delta"])
                                    )
                        if not interrupt_on:
                            # No HITL middleware installed — the graph cannot
                            # interrupt, so skip the checkpoint read.
                            break
                        resume = await resolve_interrupt(
                            agent, config, run_id=run_id, bus=bus
                        )
                        if resume is None:
                            break
                        stream_input = resume

            # Persist one assistant row per AI turn and finalize the run.
            rows = _assistant_rows(segments, conversation_id, run_id)
            async with sessionmaker()() as db:
                for row in rows:
                    db.add(row)
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
                # Refresh each row so the wire shape matches the MessageOut
                # that GET /conversations/{id} returns (DB-assigned
                # created_at on the answer row). The UI replaces its
                # streaming state with these server-authoritative objects.
                message_payloads: list[dict[str, object]] = []
                for row in rows:
                    await db.refresh(row)
                    message_payloads.append(
                        {
                            "id": str(row.id),
                            "conversation_id": str(row.conversation_id),
                            "role": row.role,
                            "content": row.content,
                            "run_id": str(row.run_id) if row.run_id else None,
                            "created_at": row.created_at.isoformat()
                            if row.created_at
                            else None,
                            "run_status": "succeeded",
                            "run_error": None,
                        }
                    )

            # Thinking rows first, answer last — the UI ends its streaming
            # bubble on the answer's complete.
            for payload in message_payloads:
                await bus.publish(
                    str(run_id),
                    {
                        "type": "message.complete",
                        "message": payload,
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
                    # Persist the segments under the same rule as success —
                    # completed turns as thinking, the partial tail as the
                    # answer. Reload shows a 'cancelled' badge via the
                    # run_status join on MessageOut.
                    for row in _assistant_rows(segments, conversation_id, run_id):
                        db.add(row)
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
                # Persist whatever partial segments the stream produced —
                # same thinking/answer rule as success. GET /conversations/{id}
                # joins Run.status/Run.error so the UI renders the answer row
                # with a 'failed' badge on reload. Empty text is fine — it
                # still marks that an attempt happened.
                for row in _assistant_rows(segments, conversation_id, run_id):
                    db.add(row)
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
