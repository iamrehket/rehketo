"""runs composite index for conversation active-run lookup

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-10 00:00:00.000000+00:00

`GET /conversations/{id}` resolves the ``active_run_id`` field by running
``SELECT id FROM runs WHERE conversation_id = ? AND status IN ('queued',
'running') ORDER BY created_at DESC LIMIT 1``.  The 0002 schema indexes
``conversation_id`` alone, so Postgres must sort the matching rows in memory
to satisfy the ``ORDER BY created_at`` — fine today, O(n) on a conversation
with a long run history. A composite index on ``(conversation_id, created_at)``
lets the planner walk the leaf in reverse order and short-circuit at the first
matching status, eliminating the sort step.

Mirrors the pattern established by 0005 for the messages table. Kept alongside
the existing ``ix_runs_conversation_id`` (from 0002) for v1; revisit dropping
the single-column index when we have profiling data on which queries use which.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_runs_conversation_id_created_at",
        "runs",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_runs_conversation_id_created_at", table_name="runs")
