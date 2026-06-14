from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.reaper import reap_stale_runs
from tests.integration._helpers import mk_running_run


async def _set_heartbeat_age(run_id: str, seconds_ago: float) -> None:
    async with sessionmaker()() as db:
        await db.execute(
            text(
                "UPDATE runs SET heartbeat_at = now() - make_interval(secs => :s) "
                "WHERE id = :r"
            ),
            {"s": seconds_ago, "r": run_id},
        )
        await db.commit()


async def test_reaps_running_run_with_stale_heartbeat(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 120)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        reaped = await reap_stale_runs(bus, threshold_seconds=60)
    finally:
        await bus.stop()

    assert UUID(run_id) in reaped
    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, error FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).one()
    assert row.status == "failed"
    assert row.error["code"] == "process_restart"


async def test_does_not_reap_fresh_heartbeat(settings_env: object, db_url: str) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 1)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        reaped = await reap_stale_runs(bus, threshold_seconds=60)
    finally:
        await bus.stop()

    assert UUID(run_id) not in reaped
    async with sessionmaker()() as db:
        row = (
            await db.execute(text("SELECT status FROM runs WHERE id=:r"), {"r": run_id})
        ).one()
    assert row.status == "running"


async def test_reaper_publishes_closure_events(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 120)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await reap_stale_runs(bus, threshold_seconds=60)

        events: list[dict] = []

        async def consume() -> None:
            async with contextlib.aclosing(bus.subscribe(run_id)) as stream:
                async for e in stream:
                    events.append(e)
                    if e["type"] == "run.ended":
                        return

        await asyncio.wait_for(consume(), timeout=10)
    finally:
        await bus.stop()

    statuses = [e for e in events if e["type"] == "run.status"]
    assert statuses[-1]["status"] == "failed"
    assert events[-1]["type"] == "run.ended"
