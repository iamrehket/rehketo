from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest_asyncio

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.db.models import Conversation, Run, User
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture
async def bus(settings_env: object, db_url: str) -> AsyncIterator[PostgresEventBus]:
    # settings_env is required: get_settings() needs the full env, and CI has
    # no .env file — db_url alone only sets DATABASE_URL.
    reset_engine_for_tests()
    b = PostgresEventBus(poll_interval=0.2)
    await b.start()
    yield b
    await b.stop()


async def _mk_run() -> str:
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@example.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="running",
            model="test-model",
        )
        # No relationship() on these models, so SQLAlchemy won't order the
        # inserts by FK — flush parent rows before their children.
        db.add(user)
        await db.flush()
        db.add(conv)
        await db.flush()
        db.add(run)
        await db.commit()
        return str(run.id)


async def _collect(
    bus: PostgresEventBus,
    run_id: str,
    n: int,
    *,
    from_sequence: int | None = None,
) -> list[dict]:
    events: list[dict] = []
    async with contextlib.aclosing(
        bus.subscribe(run_id, from_sequence=from_sequence)
    ) as stream:
        async for e in stream:
            events.append(e)
            if len(events) >= n:
                break
    return events


async def test_publish_then_subscribe_replays(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()
    for i in range(5):
        await bus.publish(run_id, {"type": "tick", "i": i})
    events = await asyncio.wait_for(_collect(bus, run_id, 5), timeout=10)
    assert [e["i"] for e in events] == [0, 1, 2, 3, 4]
    assert [e["sequence"] for e in events] == [0, 1, 2, 3, 4]
    assert all(e["run_id"] == run_id for e in events)


async def test_live_publish_wakes_subscriber(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()

    async def publisher() -> None:
        await asyncio.sleep(0.1)
        for i in range(3):
            await bus.publish(run_id, {"type": "tick", "i": i})

    task = asyncio.create_task(publisher())
    events = await asyncio.wait_for(_collect(bus, run_id, 3), timeout=10)
    await task
    assert [e["i"] for e in events] == [0, 1, 2]


async def test_from_sequence_resumes_inclusive(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()
    for i in range(5):
        await bus.publish(run_id, {"type": "tick", "i": i})
    events = await asyncio.wait_for(
        _collect(bus, run_id, 2, from_sequence=3), timeout=10
    )
    assert [e["i"] for e in events] == [3, 4]


async def test_isolation_between_runs(bus: PostgresEventBus) -> None:
    r1, r2 = await _mk_run(), await _mk_run()
    await bus.publish(r1, {"type": "tick"})
    await bus.publish(r2, {"type": "tock"})
    e1 = await asyncio.wait_for(_collect(bus, r1, 1), timeout=10)
    e2 = await asyncio.wait_for(_collect(bus, r2, 1), timeout=10)
    assert e1[0]["type"] == "tick"
    assert e2[0]["type"] == "tock"


async def test_concurrent_subscribers_same_run(bus: PostgresEventBus) -> None:
    """Two subscribers on one run share the wake set: one NOTIFY must wake
    both, and their teardowns at different times must not disturb each
    other."""
    run_id = await _mk_run()
    c1 = asyncio.create_task(_collect(bus, run_id, 3))
    c2 = asyncio.create_task(_collect(bus, run_id, 3))
    await asyncio.sleep(0.1)
    for i in range(3):
        await bus.publish(run_id, {"type": "tick", "i": i})
    e1 = await asyncio.wait_for(c1, timeout=10)
    e2 = await asyncio.wait_for(c2, timeout=10)
    assert [e["i"] for e in e1] == [0, 1, 2]
    assert [e["i"] for e in e2] == [0, 1, 2]


async def test_cross_instance_delivery(bus: PostgresEventBus) -> None:
    """Publish through one bus instance, receive through another — proves
    NOTIFY wiring between independent listener connections, the multi-process
    case in miniature. The 30s poll interval means anything delivered within
    the 10s timeout after the sentinel can only have arrived via NOTIFY."""
    other = PostgresEventBus(poll_interval=30.0)
    await other.start()
    try:
        run_id = await _mk_run()
        await bus.publish(run_id, {"type": "sentinel"})
        got_sentinel = asyncio.Event()
        received: list[dict] = []

        async def collect_after_sentinel() -> None:
            async with contextlib.aclosing(other.subscribe(run_id)) as stream:
                async for e in stream:
                    if e["type"] == "sentinel":
                        got_sentinel.set()
                        continue
                    received.append(e)
                    if len(received) >= 2:
                        return

        collector = asyncio.create_task(collect_after_sentinel())
        # Once the sentinel is consumed the collector is parked on its wake;
        # with a 30s poll, the next two events can only arrive via NOTIFY.
        await asyncio.wait_for(got_sentinel.wait(), timeout=10)
        await bus.publish(run_id, {"type": "a"})
        await bus.publish(run_id, {"type": "b"})
        await asyncio.wait_for(collector, timeout=10)
        assert [e["type"] for e in received] == ["a", "b"]
    finally:
        await other.stop()


async def test_events_survive_bus_restart(bus: PostgresEventBus) -> None:
    """The durability claim itself: a fresh instance replays everything."""
    run_id = await _mk_run()
    await bus.publish(run_id, {"type": "tick", "i": 0})
    await bus.publish(run_id, {"type": "run.ended"})
    await bus.stop()

    fresh = PostgresEventBus(poll_interval=0.2)
    await fresh.start()
    try:
        events = await asyncio.wait_for(_collect(fresh, run_id, 2), timeout=10)
        assert [e["type"] for e in events] == ["tick", "run.ended"]
    finally:
        await fresh.stop()
