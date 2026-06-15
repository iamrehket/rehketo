from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import McpServer, Skill, User
from rehketo.mcp.skills import resolve_skills


async def test_global_role_and_owned_union(settings_env, db_url, db) -> None:
    me, other = uuid4(), uuid4()
    # owner_user_id is a FK to users; insert the rows so inserts don't violate
    # the constraint.
    db.add_all([User(id=me), User(id=other)])
    await db.flush()
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(srv)
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="github",
                trigger="repos",
                kind="mcp",
                mcp_server_id=srv.id,
                allowed_roles=["User"],
                enabled=True,
            ),
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
                name="admin-only",
                trigger="x",
                kind="doc",
                instructions="body",
                allowed_roles=["Admin"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="mine",
                trigger="x",
                kind="doc",
                instructions="body",
                owner_user_id=me,
                allowed_roles=[],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="theirs",
                trigger="x",
                kind="doc",
                instructions="body",
                owner_user_id=other,
                allowed_roles=[],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="off",
                trigger="x",
                kind="doc",
                instructions="body",
                allowed_roles=["User"],
                enabled=False,
            ),
        ]
    )
    await db.commit()

    resolved = await resolve_skills(db, user_id=me, roles=["User"])
    doc_names = sorted(s.name for s in resolved.doc)
    mcp_names = sorted(s.name for s in resolved.mcp)
    # global User-role docs + my owned doc; NOT admin-only, theirs, or disabled
    assert doc_names == ["mine", "policy"]
    assert mcp_names == ["github"]


async def test_mcp_skill_dropped_when_server_not_allowed(
    settings_env, db_url, db
) -> None:
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["Admin"],
        enabled=True,  # user lacks Admin
    )
    db.add(srv)
    db.add(
        Skill(
            id=uuid4(),
            name="github",
            trigger="repos",
            kind="mcp",
            mcp_server_id=srv.id,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()
    resolved = await resolve_skills(db, user_id=uuid4(), roles=["User"])
    assert resolved.mcp == []
