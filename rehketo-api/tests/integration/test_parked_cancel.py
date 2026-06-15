from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.cancellation import request_cancel
from tests.integration._helpers import mk_pending_approval_run


async def test_request_cancel_requeues_parked_run(
    settings_env: object, db_url: str
) -> None:
    """Cancelling a parked run stamps cancel_requested_at AND flips it to queued
    so the worker's claim head finalizes it cancelled."""
    reset_engine_for_tests()
    run_id = await mk_pending_approval_run()

    async with sessionmaker()() as db:
        assert await request_cancel(db, UUID(run_id)) is True

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, cancel_requested_at FROM runs WHERE id=:r"),
                {"r": run_id},
            )
        ).one()
    assert row.status == "queued"
    assert row.cancel_requested_at is not None
