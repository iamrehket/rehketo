from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003  # returned at runtime

from sqlalchemy import text

from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker

if TYPE_CHECKING:
    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

REAP_INTERVAL_SECONDS = 30.0
REAP_THRESHOLD_SECONDS = 60.0


async def reap_stale_runs(
    bus: RunEventBus, *, threshold_seconds: float = REAP_THRESHOLD_SECONDS
) -> list[UUID]:
    """Fail every 'running' run whose heartbeat is older than the threshold —
    its owning worker died. The UPDATE is idempotent, so concurrent reapers in
    sibling workers are safe with no leader election. Publishes the terminal
    pair so a subscriber attached to a dead run's stream gets a clean close
    instead of a hang. error.code matches the old sweep so the UI/tests need no
    new vocabulary."""
    error = {"code": "process_restart", "message": "run abandoned by worker crash"}
    async with sessionmaker()() as db:
        result = await db.execute(
            text(
                "UPDATE runs SET status='failed', error=CAST(:err AS jsonb), "
                "finished_at=now() "
                "WHERE status='running' "
                "AND heartbeat_at < now() - make_interval(secs => :thr) "
                "RETURNING id"
            ),
            {"err": json.dumps(error), "thr": threshold_seconds},
        )
        ids = [row.id for row in result.all()]
        await db.commit()
    for run_id in ids:
        try:
            await bus.publish(
                str(run_id),
                {"type": "run.status", "status": "failed", "error": error},
            )
        except Exception:
            logger.warning(
                "failed to publish run.status for reaped run %s", run_id, exc_info=True
            )
        # Guaranteed terminator — isolated so a failed status publish can't
        # strand a subscriber (the row is already 'failed' and won't be reaped
        # again).
        try:
            await bus.publish(str(run_id), {"type": "run.ended"})
        except Exception:
            logger.warning(
                "failed to publish run.ended for reaped run %s", run_id, exc_info=True
            )
    if ids:
        logger.info("reaped %d stale runs", len(ids))
    return ids


async def run_reaper(
    bus: RunEventBus,
    *,
    interval_seconds: float = REAP_INTERVAL_SECONDS,
    threshold_seconds: float = REAP_THRESHOLD_SECONDS,
) -> None:
    """Reap on a fixed cadence forever. Long-lived asyncio.Task owned by the
    worker. Swallows per-pass errors so a transient DB blip costs one cycle,
    not the loop."""
    while True:
        try:
            await reap_stale_runs(bus, threshold_seconds=threshold_seconds)
        except Exception:
            logger.exception("reaper pass failed")
        await asyncio.sleep(interval_seconds)
