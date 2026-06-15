"""worker heartbeat marker + claim/reaper indexes on runs

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-14 00:00:00.000000+00:00

The agent-worker split (spec:
docs/superpowers/specs/2026-06-14-agent-worker-split-design.md) makes `runs` a
claim queue. heartbeat_at lets a reaper fail runs whose owning worker died; the
partial indexes back the claim scan (queued) and the reaper scan (running).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_runs_queued_created_at",
        "runs",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_runs_running_heartbeat",
        "runs",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_running_heartbeat", table_name="runs")
    op.drop_index("ix_runs_queued_created_at", table_name="runs")
    op.drop_column("runs", "heartbeat_at")
