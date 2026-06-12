"""Pause/resume plumbing for per-call tool approval (M3.5).

The HITL middleware interrupts the graph BEFORE an untrusted tool executes;
run_agent calls resolve_interrupt after each astream stint. The decision
travels as a durable `tool.approval_decision` event on the existing bus
(published by POST /runs/{id}/approvals/{approval_id}), so it is journaled
for transcript reload and audit, and the transport is multi-process-correct
the same way the bus already is.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import update

from rehketo.db import sessionmaker
from rehketo.db.models import Run

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus


async def resolve_interrupt(
    agent: Any, config: dict[str, Any], *, run_id: UUID, bus: RunEventBus
) -> Command[Any] | None:
    """Return a resume Command if the graph paused on tool approval, else None.

    Blocks (cancellably) until the user decides every call in the batch —
    the middleware interrupts once per model turn with ALL calls needing
    review, and the graph can only resume whole.
    """
    state = await agent.aget_state(config)
    interrupts = [i for task in state.tasks for i in task.interrupts]
    if not interrupts:
        return None
    intr = interrupts[0]
    requests = intr.value["action_requests"]
    approval_ids = [str(uuid4()) for _ in requests]
    for approval_id, request in zip(approval_ids, requests, strict=True):
        await bus.publish(
            str(run_id),
            {
                "type": "tool.approval_required",
                "approval_id": approval_id,
                "tool": request["name"],
                "arguments": request["args"],
            },
        )
    await _set_status(run_id, "pending_approval", bus)
    decisions = await wait_for_decisions(bus, str(run_id), approval_ids)
    await _set_status(run_id, "running", bus)
    # Wire vocabulary is approve/deny; the middleware's is approve/reject.
    # Reject without a message makes the middleware tell the model the tool
    # was not executed and not to retry — the spec's deny semantics.
    intr_id = intr.id
    return Command(
        resume={
            intr_id: {
                "decisions": [
                    {"type": "approve"}
                    if decisions[approval_id] == "approve"
                    else {"type": "reject"}
                    for approval_id in approval_ids
                ]
            }
        }
    )


async def wait_for_decisions(
    bus: RunEventBus, run_id: str, approval_ids: Sequence[str]
) -> dict[str, str]:
    """Collect tool.approval_decision events until the batch is resolved.

    Subscribes from sequence 0: approval ids are fresh UUIDs, so replayed
    history can never false-match, and replay-from-start needs no
    "current sequence" bookkeeping. First decision per id wins.
    """
    pending = set(approval_ids)
    decisions: dict[str, str] = {}
    stream = cast("AsyncGenerator[dict[str, object]]", bus.subscribe(run_id))
    async with contextlib.aclosing(stream) as events:
        async for event in events:
            if event.get("type") != "tool.approval_decision":
                continue
            approval_id = str(event.get("approval_id", ""))
            if approval_id not in pending:
                continue
            decisions[approval_id] = str(event["decision"])
            pending.discard(approval_id)
            if not pending:
                return decisions
    # subscribe() never returns normally; it only ends via CancelledError,
    # which propagates past this function rather than reaching these lines.
    msg = "event stream ended before approvals resolved"  # pragma: no cover  # ↑
    raise RuntimeError(msg)  # pragma: no cover  # unreachable


async def _set_status(run_id: UUID, status: str, bus: RunEventBus) -> None:
    async with sessionmaker()() as db:
        # Unconditional write is safe only because ALL of a run's status
        # writes happen in this one task today (same invariant the event
        # bus documents for its publish locks). Revisit at the M4 split.
        await db.execute(update(Run).where(Run.id == run_id).values(status=status))
        await db.commit()
    await bus.publish(str(run_id), {"type": "run.status", "status": status})
