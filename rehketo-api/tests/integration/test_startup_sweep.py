from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehketo.agent.sweep import sweep_abandoned_runs
from rehketo.db import reset_engine_for_tests
from rehketo.db.models import Conversation, Run, User
from rehketo.runs.event_bus import PostgresEventBus
from tests.integration._helpers import mk_pending_approval_run, mk_running_run


async def test_sweep_marks_running_runs_as_failed(
    settings_env: object,
    db_url: str,
    db: object,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    db_session: AsyncSession = db  # type: ignore[assignment]

    u = User(id=uuid4(), display_name="A", email="a@x")
    db_session.add(u)
    await db_session.flush()
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db_session.add(conv)
    await db_session.commit()

    run_id = uuid4()
    db_session.add(
        Run(
            id=run_id,
            conversation_id=conv.id,
            user_id=u.id,
            status="running",
            model="claude-sonnet-4-6",
            started_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await sweep_abandoned_runs(bus)
    finally:
        await bus.stop()

    # Use a fresh session to avoid SQLAlchemy identity-map returning stale state.
    fresh_engine = create_async_engine(db_url, future=True)
    maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    async with maker() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    await fresh_engine.dispose()

    assert run.status == "failed"
    assert isinstance(run.error, dict)
    assert run.error["code"] == "process_restart"


async def test_sweep_fails_pending_approval_runs(
    settings_env: object, db_url: str
) -> None:
    """A pending_approval run must be swept on startup just like running/queued.

    M3.5 scope decision: in-process approval state does not survive a restart;
    the client must receive the same clean terminal sequence it would for an
    abandoned running run.
    """
    reset_engine_for_tests()
    run_id = await mk_pending_approval_run()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await sweep_abandoned_runs(bus)

        events: list[dict] = []

        async def consume() -> None:
            async with contextlib.aclosing(bus.subscribe(run_id)) as stream:
                async for e in stream:
                    events.append(e)
                    if e["type"] == "run.ended":
                        return

        await asyncio.wait_for(consume(), timeout=10)
        statuses = [e for e in events if e["type"] == "run.status"]
        assert statuses[-1]["status"] == "failed"
        assert statuses[-1]["error"]["code"] == "process_restart"
        assert events[-1]["type"] == "run.ended"
    finally:
        await bus.stop()


async def test_sweep_publishes_closure_events(
    settings_env: object, db_url: str
) -> None:
    """A client reconnecting to a dead run's stream must get the normal
    terminal sequence (run.status=failed + run.ended), not a hang."""
    reset_engine_for_tests()
    run_id = await mk_running_run()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await sweep_abandoned_runs(bus)

        events: list[dict] = []

        async def consume() -> None:
            async with contextlib.aclosing(bus.subscribe(run_id)) as stream:
                async for e in stream:
                    events.append(e)
                    if e["type"] == "run.ended":
                        return

        await asyncio.wait_for(consume(), timeout=10)
        statuses = [e for e in events if e["type"] == "run.status"]
        assert statuses[-1]["status"] == "failed"
        assert statuses[-1]["error"]["code"] == "process_restart"
        assert events[-1]["type"] == "run.ended"
    finally:
        await bus.stop()
