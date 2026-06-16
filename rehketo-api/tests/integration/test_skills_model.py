from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from rehketo.db.models import McpServer, Skill, User


async def test_doc_skill_persists(settings_env, db_url, db) -> None:
    db.add(
        Skill(
            id=uuid4(),
            name="expense-policy",
            display_name="Expense policy",
            trigger="use when answering questions about reimbursement",
            kind="doc",
            instructions="# Expense policy\nReimburse within 30 days.",
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()


async def test_mcp_skill_persists(settings_env, db_url, db) -> None:
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://github.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(srv)
    await db.commit()
    db.add(
        Skill(
            id=uuid4(),
            name="github",
            trigger="use when working with GitHub repos, PRs, or issues",
            kind="mcp",
            mcp_server_id=srv.id,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()


async def test_doc_skill_without_instructions_violates_check(
    settings_env, db_url, db
) -> None:
    db.add(
        Skill(
            id=uuid4(),
            name="bad-doc",
            trigger="x",
            kind="doc",
            instructions=None,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_mcp_skill_without_server_violates_check(
    settings_env, db_url, db
) -> None:
    db.add(
        Skill(
            id=uuid4(),
            name="bad-mcp",
            trigger="x",
            kind="mcp",
            mcp_server_id=None,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()


def _doc(name: str, *, owner: UUID | None = None) -> Skill:
    return Skill(
        id=uuid4(),
        name=name,
        trigger="t",
        kind="doc",
        instructions="body",
        owner_user_id=owner,
        allowed_roles=[],
        enabled=True,
    )


async def test_user_may_reuse_a_global_name(settings_env, db_url, db) -> None:
    me = uuid4()
    db.add(User(id=me))
    await db.flush()
    db.add(_doc("research"))  # global
    db.add(_doc("research", owner=me))  # owned, same name — allowed
    await db.commit()  # must not raise


async def test_two_globals_cannot_share_a_name(settings_env, db_url, db) -> None:
    db.add(_doc("research"))
    db.add(_doc("research"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_one_user_cannot_duplicate_their_own_name(
    settings_env, db_url, db
) -> None:
    me = uuid4()
    db.add(User(id=me))
    await db.flush()
    db.add(_doc("research", owner=me))
    db.add(_doc("research", owner=me))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_two_users_may_share_a_name(settings_env, db_url, db) -> None:
    alice, bob = uuid4(), uuid4()
    db.add(User(id=alice))
    db.add(User(id=bob))
    await db.flush()
    db.add(_doc("research", owner=alice))
    db.add(_doc("research", owner=bob))
    await db.commit()  # must not raise
