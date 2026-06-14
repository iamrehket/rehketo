from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003  # used at runtime in the dataclass

from sqlalchemy import text

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

RUN_QUEUED_CHANNEL = "run_queued"


@dataclass(frozen=True)
class ClaimedRun:
    id: UUID
    conversation_id: UUID
    user_id: UUID
    cancel_requested_at: datetime | None


async def claim_next_run(db: AsyncSession) -> ClaimedRun | None:
    """Atomically claim one queued run. The UPDATE flips status to 'running'
    and stamps heartbeat_at in the same statement, so a just-claimed run can
    never look stale to a reaper. FOR UPDATE SKIP LOCKED lets N workers claim
    disjoint runs without blocking each other. Silent: no event is published —
    run_agent publishes run.status=running, keeping that the single source of
    the event. started_at is preserved across a resume via COALESCE."""
    row = (
        await db.execute(
            text(
                "UPDATE runs SET status='running', heartbeat_at=now(), "
                "started_at=COALESCE(started_at, now()) "
                "WHERE id = (SELECT id FROM runs WHERE status='queued' "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING id, conversation_id, user_id, cancel_requested_at"
            )
        )
    ).one_or_none()
    await db.commit()
    if row is None:
        return None
    return ClaimedRun(
        id=row.id,
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        cancel_requested_at=row.cancel_requested_at,
    )


async def notify_run_queued(db: AsyncSession, run_id: UUID) -> None:
    """Ring the doorbell so an idle worker claims promptly. Call within the
    same transaction that inserts/flips the run to 'queued' — postgres delivers
    NOTIFY on commit, so the wake can never precede the row. A missed NOTIFY
    costs only latency: workers also poll."""
    await db.execute(
        text("SELECT pg_notify(:chan, :rid)"),
        {"chan": RUN_QUEUED_CHANNEL, "rid": str(run_id)},
    )
