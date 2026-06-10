from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import update

from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker
from rehketo.db.models import Run

if TYPE_CHECKING:
    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)


async def sweep_abandoned_runs(bus: RunEventBus) -> None:
    """On startup, mark any runs stuck in `running` or `queued` as failed,
    and publish the terminal event pair so any client still subscribed to a
    dead run's stream gets a clean close instead of a hang.

    Deployment constraint: this sweep assumes it owns ALL non-terminal runs —
    with multiple uvicorn workers, one worker restarting would force-fail
    runs alive in its siblings. Single-process deployment remains required
    until run ownership lands with the agent-worker split.

    Anything in those states at startup was abandoned by the previous
    process; the checkpointer may still have state but we do not resume yet
    (that arrives with the agent worker split).

    Known gap: if the process dies between the UPDATE committing and the
    publishes completing, those runs are failed in the DB but have no closure
    events, and later sweeps will not revisit them (no longer queued/running).
    A subscriber attached to such a run waits forever; closing that hole needs
    a subscribe-time terminal-status check, which belongs to the bus, not the
    sweep.
    """
    error = {
        "code": "process_restart",
        "message": "run abandoned by process restart",
    }
    async with sessionmaker()() as db:
        result = await db.execute(
            update(Run)
            .where(Run.status.in_(["queued", "running"]))
            .values(
                status="failed",
                error=error,
                finished_at=datetime.now(UTC),
            )
            .returning(Run.id)
        )
        ids = [row[0] for row in result.all()]
        await db.commit()
    for run_id in ids:
        try:
            await bus.publish(
                str(run_id),
                {"type": "run.status", "status": "failed", "error": error},
            )
            await bus.publish(str(run_id), {"type": "run.ended"})
        except Exception:
            # Best-effort: the run is already failed in the DB; blocking app
            # startup over a closure event is worse than a client waiting for
            # its next reconnect.
            logger.warning(
                "failed to publish closure events for run %s",
                run_id,
                exc_info=True,
            )
    if ids:
        logger.info("swept %d abandoned runs on startup", len(ids))
