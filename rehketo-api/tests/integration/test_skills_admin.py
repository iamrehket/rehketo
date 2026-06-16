from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import McpServer, Skill, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "Admin") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    return str(sid), issue_csrf_token(str(sid))


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


_DOC = {
    "name": "policy",
    "kind": "doc",
    "trigger": "reimburse",
    "instructions": "Steps.",
}


async def test_admin_doc_crud_roundtrip(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        assert r.status_code == 201
        skill_id = r.json()["id"]
        assert r.json()["kind"] == "doc"

        r = await c.get("/admin/skills", cookies={SESSION_COOKIE: sid})
        assert skill_id in [s["id"] for s in r.json()["items"]]

        r = await c.patch(
            f"/admin/skills/{skill_id}", json={"enabled": False}, **_auth(sid, csrf)
        )
        assert r.json()["enabled"] is False

        r = await c.delete(f"/admin/skills/{skill_id}", **_auth(sid, csrf))
        assert r.status_code == 204


async def test_admin_create_mcp_skill(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(srv)
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/skills",
            json={
                "name": "github",
                "kind": "mcp",
                "trigger": "repos",
                "mcp_server_id": str(srv.id),
                "allowed_roles": ["User"],
            },
            **_auth(sid, csrf),
        )
        assert r.status_code == 201
        assert r.json()["mcp_server_id"] == str(srv.id)


async def test_kind_backing_xor_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/skills",
            json={"name": "bad", "kind": "doc", "trigger": "t"},
            **_auth(sid, csrf),
        )
        assert r.status_code == 422
        r = await c.post(
            "/admin/skills",
            json={"name": "bad2", "kind": "mcp", "trigger": "t", "instructions": "x"},
            **_auth(sid, csrf),
        )
        assert r.status_code == 422


async def test_non_admin_is_403(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db, role="User")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.get("/admin/skills", cookies={SESSION_COOKIE: sid})
        ).status_code == 403
        assert (
            await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        ).status_code == 403
        # 403 from the perms gate fires before any 404 lookup.
        assert (
            await c.patch(
                f"/admin/skills/{uuid4()}", json={"enabled": False}, **_auth(sid, csrf)
            )
        ).status_code == 403
        assert (
            await c.delete(f"/admin/skills/{uuid4()}", **_auth(sid, csrf))
        ).status_code == 403


async def test_duplicate_global_name_is_409(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        ).status_code == 201
        r = await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        assert r.status_code == 409


async def test_patch_ignores_name_and_kind(settings_env, db_url, db) -> None:
    """PATCH with name/kind in body must not mutate identity fields."""
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        assert r.status_code == 201
        skill_id = r.json()["id"]

        r = await c.patch(
            f"/admin/skills/{skill_id}",
            json={"name": "hacked", "kind": "mcp", "trigger": "x"},
            **_auth(sid, csrf),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "policy"
        assert body["kind"] == "doc"
        assert body["trigger"] == "x"


async def test_mcp_skill_bad_server_id_is_400(settings_env, db_url, db) -> None:
    """Creating an mcp-skill with a nonexistent mcp_server_id returns 400."""
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/skills",
            json={
                "name": "ghost",
                "kind": "mcp",
                "trigger": "repos",
                "mcp_server_id": str(uuid4()),
            },
            **_auth(sid, csrf),
        )
        assert r.status_code == 400
        assert "mcp_server_id" in r.json()["error"]["message"]


async def test_patch_delete_user_owned_skill_is_404(settings_env, db_url, db) -> None:
    """Admin PATCH/DELETE on a user-owned skill returns 404 (global-only surface)."""
    sid, csrf = await _seed_session(db)
    # Seed a user-owned skill directly in the DB.
    owner = User(id=uuid4(), display_name="Owner", email=f"{uuid4()}@example.com")
    db.add(owner)
    await db.flush()
    private_skill = Skill(
        id=uuid4(),
        name="private-skill",
        kind="doc",
        trigger="something",
        instructions="Private instructions.",
        owner_user_id=owner.id,
        allowed_roles=[],
        enabled=True,
    )
    db.add(private_skill)
    await db.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/admin/skills/{private_skill.id}",
            json={"enabled": False},
            **_auth(sid, csrf),
        )
        assert r.status_code == 404

        r = await c.delete(f"/admin/skills/{private_skill.id}", **_auth(sid, csrf))
        assert r.status_code == 404
