"""server-side store for the OAuth post-login `next` path

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-09 00:00:00.000000+00:00

Moves the user-supplied `next` path out of a browser cookie (which CodeQL
flagged as py/cookie-injection) into a single-use, TTL-bounded row keyed by
the login `state` token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_pending_logins",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("next_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )


def downgrade() -> None:
    op.drop_table("oauth_pending_logins")
