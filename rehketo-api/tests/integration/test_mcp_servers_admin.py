from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import McpServer, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "Admin") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(sid), issue_csrf_token(str(sid))


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


_CREATE_BODY = {
    "name": "github",
    "url": "https://mcp.example.com/mcp",
    "auth_token": "secret-token",
    "allowed_roles": ["Admin", "User"],
    "enabled": True,
}


async def test_crud_roundtrip_token_write_only(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        created = r.json()
        assert created["name"] == "github"
        assert created["has_auth_token"] is True
        assert "auth_token" not in created
        server_id = created["id"]

        r = await c.get("/admin/mcp-servers", cookies={SESSION_COOKIE: sid})
        assert r.status_code == 200
        assert [s["id"] for s in r.json()["items"]] == [server_id]

        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"enabled": False, "auth_token": None},
            **_auth(sid, csrf),
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["has_auth_token"] is False

        # PATCH that omits auth_token leaves it unchanged.
        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"auth_token": "tok2"},
            **_auth(sid, csrf),
        )
        assert r.json()["has_auth_token"] is True
        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"enabled": True},
            **_auth(sid, csrf),
        )
        assert r.json()["has_auth_token"] is True

        r = await c.delete(f"/admin/mcp-servers/{server_id}", **_auth(sid, csrf))
        assert r.status_code == 204

    row = (
        await db.execute(select(McpServer).where(McpServer.id == server_id))
    ).scalar_one_or_none()
    assert row is None


async def test_token_is_encrypted_at_rest(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
    row = (
        await db.execute(select(McpServer).where(McpServer.name == "github"))
    ).scalar_one()
    assert row.auth_token_ct is not None
    assert b"secret-token" not in row.auth_token_ct


async def test_non_admin_is_403(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db, role="User")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/admin/mcp-servers", cookies={SESSION_COOKIE: sid})
        assert r.status_code == 403
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 403


async def test_duplicate_name_is_409(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 409


async def test_bad_url_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/mcp-servers",
            json={**_CREATE_BODY, "url": "not-a-url"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422


async def test_non_slug_name_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/mcp-servers",
            json={**_CREATE_BODY, "name": "My Server"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422


async def test_empty_auth_token_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/mcp-servers",
            json={**_CREATE_BODY, "auth_token": ""},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422


async def test_unknown_id_patch_and_delete_are_404(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    unknown = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/admin/mcp-servers/{unknown}",
            json={"enabled": False},
            **_auth(sid, csrf),
        )
        assert r.status_code == 404
        r = await c.delete(f"/admin/mcp-servers/{unknown}", **_auth(sid, csrf))
        assert r.status_code == 404


async def test_double_underscore_in_name_is_422(settings_env, db_url, db) -> None:
    """Server name containing __ collides with the tool-prefix separator — rejected."""
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/mcp-servers",
            json={**_CREATE_BODY, "name": "a__b"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422
