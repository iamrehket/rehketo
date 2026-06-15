"""skills registry (M4.5 discovery)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-15 00:00:00.000000+00:00

A skill is a discovery card backed by either an MCP server (kind='mcp', one
skill per server in v1) or an authored markdown doc (kind='doc'). owner_user_id
NULL means global; the column exists now so user-scoped skills (a later slice)
need no schema change. The kind/backing check keeps the two shapes honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mcp_server_id", sa.UUID(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
        sa.Column("allowed_roles", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.CheckConstraint("kind in ('mcp','doc')", name="skills_kind_enum"),
        sa.CheckConstraint(
            "(kind = 'mcp' AND mcp_server_id IS NOT NULL AND instructions IS NULL) "
            "OR (kind = 'doc' AND instructions IS NOT NULL AND mcp_server_id IS NULL)",
            name="skills_kind_backing",
        ),
    )
    op.create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_skills_owner_user_id", table_name="skills")
    op.drop_table("skills")
