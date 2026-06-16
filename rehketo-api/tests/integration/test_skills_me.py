from __future__ import annotations

from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Skill, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "User") -> tuple[str, str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    return str(u.id), str(sid), issue_csrf_token(str(sid))


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


async def test_lists_global_and_owned_with_flags(settings_env, db_url, db) -> None:
    user_id, sid, _csrf = await _seed_session(db)
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="policy",
                trigger="reimburse",
                kind="doc",
                instructions="body",
                allowed_roles=["User"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="mine",
                trigger="t",
                kind="doc",
                instructions="body",
                owner_user_id=UUID(user_id),
                allowed_roles=[],
                enabled=True,
            ),
        ]
    )
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/skills", cookies={SESSION_COOKIE: sid})
    assert r.status_code == 200
    items = {s["name"]: s for s in r.json()["items"]}
    assert items["policy"]["source"] == "global"
    assert items["policy"]["editable"] is False
    assert items["mine"]["source"] == "owned"
    assert items["mine"]["editable"] is True


_DOC_BODY = {
    "name": "my-notes",
    "trigger": "use for my notes",
    "instructions": "Steps.",
}


async def test_create_edit_delete_own_doc_skill(settings_env, db_url, db) -> None:
    _user_id, sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        created = r.json()
        assert created["kind"] == "doc"
        assert created["source"] == "owned"
        assert created["editable"] is True
        skill_id = created["id"]

        r = await c.patch(
            f"/me/skills/{skill_id}", json={"trigger": "updated"}, **_auth(sid, csrf)
        )
        assert r.status_code == 200
        assert r.json()["trigger"] == "updated"

        r = await c.delete(f"/me/skills/{skill_id}", **_auth(sid, csrf))
        assert r.status_code == 204


async def test_cannot_edit_a_global_skill(settings_env, db_url, db) -> None:
    _user_id, sid, csrf = await _seed_session(db)
    glob = Skill(
        id=uuid4(),
        name="policy",
        trigger="t",
        kind="doc",
        instructions="b",
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(glob)
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/me/skills/{glob.id}", json={"trigger": "x"}, **_auth(sid, csrf)
        )
        assert r.status_code == 404  # not owned -> not found
        r = await c.delete(f"/me/skills/{glob.id}", **_auth(sid, csrf))
        assert r.status_code == 404


async def test_cannot_edit_another_users_skill(settings_env, db_url, db) -> None:
    _user_id, sid, csrf = await _seed_session(db)
    other = User(id=uuid4(), display_name="Other", email=f"{uuid4()}@example.com")
    db.add(other)
    await db.flush()
    theirs = Skill(
        id=uuid4(),
        name="theirs",
        trigger="t",
        kind="doc",
        instructions="b",
        owner_user_id=other.id,
        allowed_roles=[],
        enabled=True,
    )
    db.add(theirs)
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/me/skills/{theirs.id}", json={"trigger": "x"}, **_auth(sid, csrf)
        )
        assert r.status_code == 404
        r = await c.delete(f"/me/skills/{theirs.id}", **_auth(sid, csrf))
        assert r.status_code == 404


async def test_duplicate_own_name_is_409(settings_env, db_url, db) -> None:
    _user_id, sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))
        ).status_code == 201
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))
        assert r.status_code == 409


async def test_author_without_capability_is_403(settings_env, db_url, db) -> None:
    # A user seeded with no roles holds no actions, including chat.author_skill.
    u = User(id=uuid4(), display_name="No", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    csrf = issue_csrf_token(str(sid))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(str(sid), csrf))
        assert r.status_code == 403
