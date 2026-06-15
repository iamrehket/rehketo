from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from rehketo.db.models import McpServer, Skill


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
