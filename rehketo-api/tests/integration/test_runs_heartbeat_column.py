from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.db.models import Conversation, Run, User


async def test_run_persists_heartbeat_at(settings_env: object, db_url: str) -> None:
    reset_engine_for_tests()
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@x.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        db.add(user)
        await db.flush()
        db.add(conv)
        await db.flush()
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="queued",
            model="m",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async with sessionmaker()() as db:
        row = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert row.heartbeat_at is None  # column exists, defaults null
