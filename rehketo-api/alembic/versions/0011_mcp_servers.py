"""admin-configured MCP servers

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11 00:00:00.000000+00:00

Server list is data, never code (north star): rows are managed live from
the admin API, no restart to reconfigure. auth_token_ct follows the
sessions.refresh_token_ct pattern — Fernet ciphertext bytes, never
returned by the API. allowed_roles is JSONB-on-the-row because roles are
plain strings in ROLE_PERMISSIONS, not DB entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("auth_token_ct", sa.LargeBinary(), nullable=True),
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
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
