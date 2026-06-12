from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import text

from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Run, User
from rehketo.runs.event_bus import PostgresEventBus


async def _seed_run(db) -> str:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="running",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()
    return str(run.id)


async def test_concurrent_publishes_get_distinct_sequences(
    settings_env, db_url, db
) -> None:
    run_id = await _seed_run(db)
    bus = PostgresEventBus()

    await asyncio.gather(
        *(
            bus.publish(run_id, {"type": "tool.call", "call_id": f"c{i}"})
            for i in range(20)
        )
    )

    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT sequence FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": run_id},
            )
        ).all()
    assert [r.sequence for r in rows] == list(range(20))


async def test_lock_is_dropped_after_run_ended(settings_env, db_url, db) -> None:
    run_id = await _seed_run(db)
    bus = PostgresEventBus()
    await bus.publish(run_id, {"type": "run.status", "status": "running"})
    assert run_id in bus._publish_locks
    await bus.publish(run_id, {"type": "run.ended"})
    assert run_id not in bus._publish_locks
