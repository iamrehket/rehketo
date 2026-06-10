from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select, update

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.db.models import Run
from rehketo.runs.cancellation import RunControlListener, request_cancel
from rehketo.runs.registry import RunTaskRegistry
from tests.integration._helpers import mk_running_run


async def test_cancel_reaches_task_via_control_channel(
    settings_env: object, db_url: str
) -> None:
    """The cancel request travels: DB column + NOTIFY -> listener -> registry
    -> task.cancel(). The requester shares no memory with the task holder —
    this is the cross-process path in miniature."""
    reset_engine_for_tests()
    run_id = await mk_running_run()

    registry = RunTaskRegistry()
    cancelled = asyncio.Event()

    async def fake_run() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(fake_run())
    registry.register(UUID(run_id), task)

    listener = RunControlListener(registry)
    await listener.start()
    try:
        async with sessionmaker()() as db:
            await request_cancel(db, UUID(run_id))
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        task.cancel()
        await listener.stop()

    # The durable record exists regardless of delivery.
    async with sessionmaker()() as db:
        run = (await db.execute(select(Run).where(Run.id == UUID(run_id)))).scalar_one()
        assert run.cancel_requested_at is not None


async def test_request_cancel_refuses_terminal_run(
    settings_env: object, db_url: str
) -> None:
    """The terminal guard lives in the UPDATE: a finished run is never
    stamped, so the future agent-worker consumer can trust the column."""
    reset_engine_for_tests()
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await db.execute(
            update(Run).where(Run.id == UUID(run_id)).values(status="succeeded")
        )
        await db.commit()

    async with sessionmaker()() as db:
        assert await request_cancel(db, UUID(run_id)) is False

    async with sessionmaker()() as db:
        run = (await db.execute(select(Run).where(Run.id == UUID(run_id)))).scalar_one()
        assert run.cancel_requested_at is None
