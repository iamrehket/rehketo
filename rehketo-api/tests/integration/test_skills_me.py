from __future__ import annotations

from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import SESSION_COOKIE
from rehketo.auth.sessions import create_session
from rehketo.db.models import Skill, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "User") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    return str(u.id), str(sid)


async def test_lists_global_and_owned_with_flags(settings_env, db_url, db) -> None:
    user_id, sid = await _seed_session(db)
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
