from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

import pytest
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.claim import notify_run_queued
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.worker import run_worker
from tests.integration._helpers import (
    FakeStreamingAgent,
    make_fake_build_agent,
    mk_running_run,
)


async def _queue_run() -> str:
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET status='queued', started_at=NULL WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()
    return run_id


async def _wait_status(run_id: str, status: str, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            async with sessionmaker()() as db:
                row = (
                    await db.execute(
                        text("SELECT status FROM runs WHERE id=:r"), {"r": run_id}
                    )
                ).one()
            if row.status == status:
                return
            await asyncio.sleep(0.05)


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_mod, "build_agent", make_fake_build_agent(FakeStreamingAgent(("hi",)))
    )


async def test_worker_claims_and_runs_a_queued_run(
    settings_env: object, db_url: str, fake_agent: None
) -> None:
    reset_engine_for_tests()
    run_id = await _queue_run()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    worker = asyncio.create_task(run_worker(bus, poll_interval=0.5))
    try:
        async with sessionmaker()() as db:
            await notify_run_queued(db, UUID(run_id))
            await db.commit()
        await _wait_status(run_id, "succeeded")
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await bus.stop()


async def test_worker_finalizes_precancelled_run_without_executing(
    settings_env: object, db_url: str
) -> None:
    """A run cancelled while parked: cancel_requested_at is set and status is
    queued. The worker finalizes 'cancelled' at the claim head — build_agent is
    never called (no monkeypatch installed; a call would explode)."""
    reset_engine_for_tests()
    run_id = await _queue_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET cancel_requested_at=now() WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    worker = asyncio.create_task(run_worker(bus, poll_interval=0.5))
    try:
        await _wait_status(run_id, "cancelled")
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await bus.stop()

    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id=:r ORDER BY sequence"
                ),
                {"r": run_id},
            )
        ).all()
    types = [r.payload["type"] for r in rows]
    assert types[-1] == "run.ended"
