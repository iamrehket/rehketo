from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.cancellation import request_cancel
from rehketo.runs.heartbeat import beat
from tests.integration._helpers import mk_running_run


async def test_beat_advances_heartbeat_and_reports_no_cancel(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()

    cancel_requested = await beat(UUID(run_id))
    assert cancel_requested is False

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT heartbeat_at FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).one()
    assert row.heartbeat_at is not None


async def test_beat_reports_pending_cancel(settings_env: object, db_url: str) -> None:
    """The heartbeat re-reads cancel_requested_at as a backstop against a lost
    control NOTIFY."""
    reset_engine_for_tests()
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await request_cancel(db, UUID(run_id))

    assert await beat(UUID(run_id)) is True
