"""Pause/resume plumbing for per-call tool approval.

The HITL middleware interrupts the graph BEFORE an untrusted tool executes.
On the first encounter the worker publishes one durable tool.approval_required
per call (carrying the interrupt id for correlation), parks the run at
pending_approval, and releases its slot. When the decision arrives the run is
re-queued; on re-claim build_resume_command reconstructs the resume Command
from the journaled approval_required + approval_decision events. Decisions are
the durable source of truth, so resume is correct across processes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import text

from rehketo.db import sessionmaker

if TYPE_CHECKING:
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus


def _interrupt(state: Any) -> Any | None:
    interrupts = [i for task in state.tasks for i in task.interrupts]
    return interrupts[0] if interrupts else None


async def park_on_interrupt(
    agent: Any, config: dict[str, Any], *, run_id: UUID, bus: RunEventBus
) -> bool:
    """If the graph paused on approval, publish one approval_required per call,
    set pending_approval, and return True (the caller releases the run). Return
    False if there is no interrupt (the turn finished). Idempotent against a
    re-encounter: if approval_required already exists for this interrupt id we
    do not re-publish."""
    state = await agent.aget_state(config)
    intr = _interrupt(state)
    if intr is None:
        return False
    if not await _required_ids(run_id, intr.id):
        requests = intr.value["action_requests"]
        for request in requests:
            await bus.publish(
                str(run_id),
                {
                    "type": "tool.approval_required",
                    "approval_id": str(uuid4()),
                    "interrupt_id": intr.id,
                    "tool": request["name"],
                    "arguments": request["args"],
                },
            )
    await _set_status(run_id, "pending_approval", bus)
    return True


async def build_resume_command(
    agent: Any, config: dict[str, Any], *, run_id: UUID
) -> Command[Any] | None:
    """On re-claim, reconstruct the resume Command from durable events. Returns
    None if the checkpoint no longer holds an interrupt (already resumed). Pure
    read: it does NOT publish run.status=running — run_agent's start block
    already did when the re-claimed run flipped to running, so the resume emits
    exactly one running event."""
    state = await agent.aget_state(config)
    intr = _interrupt(state)
    if intr is None:
        return None
    ids = await _required_ids(run_id, intr.id)  # publish order == request order
    decisions = await _decisions_for(run_id, ids)
    # Wire vocabulary approve/deny -> middleware approve/reject. A bare reject
    # tells the model the tool was not executed and not to retry (deny).
    return Command(
        resume={
            intr.id: {
                "decisions": [
                    {"type": "approve"}
                    if decisions.get(approval_id) == "approve"
                    else {"type": "reject"}
                    for approval_id in ids
                ]
            }
        }
    )


async def _required_ids(run_id: UUID, interrupt_id: str) -> list[str]:
    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload->>'approval_id' AS aid FROM run_events "
                    "WHERE run_id=:r AND payload->>'type'='tool.approval_required' "
                    "AND payload->>'interrupt_id'=:i ORDER BY sequence"
                ),
                {"r": str(run_id), "i": interrupt_id},
            )
        ).all()
    return [row.aid for row in rows]


async def _decisions_for(run_id: UUID, approval_ids: list[str]) -> dict[str, str]:
    if not approval_ids:
        return {}
    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload->>'approval_id' AS aid, "
                    "payload->>'decision' AS dec FROM run_events "
                    "WHERE run_id=:r AND payload->>'type'='tool.approval_decision' "
                    "AND payload->>'approval_id' = ANY(:ids) ORDER BY sequence"
                ),
                {"r": str(run_id), "ids": approval_ids},
            )
        ).all()
    # First decision per id wins.
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(row.aid, row.dec)
    return out


async def _set_status(run_id: UUID, status: str, bus: RunEventBus) -> None:
    async with sessionmaker()() as db:
        # Safe under multi-worker: a parked run has exactly one claimer at a
        # time — the SKIP LOCKED claim is the mutex.
        await db.execute(
            text("UPDATE runs SET status=:s WHERE id=:r"),
            {"s": status, "r": str(run_id)},
        )
        await db.commit()
    await bus.publish(str(run_id), {"type": "run.status", "status": status})
