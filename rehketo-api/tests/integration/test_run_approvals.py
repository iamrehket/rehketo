"""POST /runs/{run_id}/approvals/{approval_id} — guards and the happy path.
The endpoint validates (owner, pending status, known + undecided approval id)
then publishes tool.approval_decision to the durable bus; the waiting run
task picks it up from its own subscription (covered by
test_run_agent_approval.py)."""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Run, User, UserRole
from rehketo.main import create_app
from rehketo.runs.event_bus import PostgresEventBus


async def _seed_pending_run(db, *, role: str = "User") -> tuple[str, str, str, str]:
    """User + conversation + pending_approval run + one approval_required
    event. Returns (sid, csrf, run_id, approval_id)."""
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    if role:
        db.add(UserRole(user_id=u.id, role=role))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="pending_approval",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()
    approval_id = str(uuid4())
    await PostgresEventBus().publish(
        str(run.id),
        {
            "type": "tool.approval_required",
            "approval_id": approval_id,
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))
    return str(sid), csrf, str(run.id), approval_id


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


async def _decision_events(run_id: str) -> list[dict]:
    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.approval_decision' "
                    "ORDER BY sequence"
                ),
                {"rid": run_id},
            )
        ).all()
    return [r.payload for r in rows]


async def test_approve_publishes_decision_event(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 204
    events = await _decision_events(run_id)
    assert len(events) == 1
    assert events[0]["approval_id"] == approval_id
    assert events[0]["decision"] == "approve"


async def test_duplicate_decision_409(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "deny"},
            **_auth(sid, csrf),
        )
        r2 = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r1.status_code == 204
    assert r2.status_code == 409
    assert len(await _decision_events(run_id)) == 1  # first decision wins


async def test_unknown_approval_id_404(settings_env, db_url, db) -> None:
    sid, csrf, run_id, _approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{uuid4()}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 404


async def test_run_not_pending_409(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    async with sessionmaker()() as s:
        await s.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :rid"),
            {"rid": run_id},
        )
        await s.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 409


async def test_other_users_run_404(settings_env, db_url, db) -> None:
    _sid, _csrf, run_id, approval_id = await _seed_pending_run(db)
    other = User(id=uuid4(), display_name="Eve", email=f"{uuid4()}@example.com")
    db.add(other)
    await db.flush()
    db.add(UserRole(user_id=other.id, role="User"))
    await db.commit()
    sid2 = await create_session(
        db,
        user_id=other.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf2 = issue_csrf_token(str(sid2))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(str(sid2), csrf2),
        )
    assert r.status_code == 404


async def test_invalid_decision_value_422(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "maybe"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422
