from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import SESSION_COOKIE
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, Run, User, UserRole
from rehketo.main import create_app


async def test_detail_happy(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add_all([u, UserRole(user_id=u.id, role="User")])
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="hi")
    db.add(conv)
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(conv.id)
    assert body["title"] == "hi"
    assert body["messages"] == []
    assert body["active_run_id"] is None


async def test_detail_404_for_other_users_conversation(
    settings_env, db_url, db
) -> None:
    alice = User(id=uuid4(), display_name="A", email="a@x")
    bob = User(id=uuid4(), display_name="B", email="b@x")
    db.add_all([alice, bob, UserRole(user_id=alice.id, role="User")])
    await db.commit()
    bob_conv = Conversation(id=uuid4(), user_id=bob.id, title="his")
    db.add(bob_conv)
    await db.commit()
    sid = await create_session(
        db,
        user_id=alice.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            f"/conversations/{bob_conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
    assert r.status_code == 404


async def test_detail_exposes_active_run_id(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add_all([u, UserRole(user_id=u.id, role="User")])
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="hi")
    db.add(conv)
    await db.flush()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="running",
        model="test-model",
    )
    db.add(run)
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    body = r.json()
    assert body["active_run_id"] == str(run.id)


async def test_detail_active_run_id_null_when_terminal(
    settings_env, db_url, db
) -> None:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add_all([u, UserRole(user_id=u.id, role="User")])
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="hi")
    db.add(conv)
    await db.flush()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="test-model",
    )
    db.add(run)
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    body = r.json()
    assert body["active_run_id"] is None
