"""per-call tool approval: mcp_servers.auto_approve + pending_approval status

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-12 00:00:00.000000+00:00

auto_approve defaults false for ALL rows including existing ones (spec:
approval-required is the safe default; admins flip trusted servers).
runs.status gains 'pending_approval' — a non-terminal state between
'running' stints while the task waits for a user decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STATUSES_OLD = "('queued','running','succeeded','failed','cancelled')"
_STATUSES_NEW = (
    "('queued','running','pending_approval','succeeded','failed','cancelled')"
)


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column(
            "auto_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.drop_constraint("runs_status_enum", "runs", type_="check")
    op.create_check_constraint("runs_status_enum", "runs", f"status in {_STATUSES_NEW}")


def downgrade() -> None:
    op.drop_constraint("runs_status_enum", "runs", type_="check")
    op.create_check_constraint("runs_status_enum", "runs", f"status in {_STATUSES_OLD}")
    op.drop_column("mcp_servers", "auto_approve")
