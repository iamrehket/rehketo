from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import User, UserRole
from rehketo.main import create_app


async def _seed_session(db, email: str | None = None) -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=email or f"{uuid4()}@example.com")
    db.add_all([u, UserRole(user_id=u.id, role="User")])
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(sid), issue_csrf_token(str(sid))


async def test_get_preferences_empty_without_row(settings_env, db_url, db) -> None:
    sid, _ = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
    assert r.status_code == 200
    assert r.json() == {"custom_instructions": ""}


async def test_put_creates_then_updates(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cookies = {SESSION_COOKIE: sid, CSRF_COOKIE: csrf}
        headers = {CSRF_HEADER: csrf}

        r = await c.put(
            "/me/preferences",
            cookies=cookies,
            headers=headers,
            json={"custom_instructions": "Answer in haiku."},
        )
        assert r.status_code == 200
        assert r.json() == {"custom_instructions": "Answer in haiku."}

        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
        assert r.json() == {"custom_instructions": "Answer in haiku."}

        r = await c.put(
            "/me/preferences",
            cookies=cookies,
            headers=headers,
            json={"custom_instructions": "Be terse."},
        )
        assert r.status_code == 200

        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
        assert r.json() == {"custom_instructions": "Be terse."}


async def test_put_over_limit_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put(
            "/me/preferences",
            cookies={SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"custom_instructions": "x" * 4000},
        )
        assert r.status_code == 200

        r = await c.put(
            "/me/preferences",
            cookies={SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"custom_instructions": "x" * 4001},
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_get_preferences_unauthenticated_is_401(settings_env, db_url, db) -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/preferences")
    assert r.status_code == 401


async def test_preferences_are_per_user(settings_env, db_url, db) -> None:
    sid_a, csrf_a = await _seed_session(db)
    sid_b, _ = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put(
            "/me/preferences",
            cookies={SESSION_COOKIE: sid_a, CSRF_COOKIE: csrf_a},
            headers={CSRF_HEADER: csrf_a},
            json={"custom_instructions": "User A's secret instructions."},
        )
        assert r.status_code == 200

        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid_b})
    assert r.status_code == 200
    assert r.json() == {"custom_instructions": ""}
