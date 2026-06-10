"""durable cancel request marker on runs

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10 00:00:00.000000+00:00

Cancellation moves from an in-process-only registry call to a durable
column + NOTIFY doorbell so it works across processes (spec:
docs/superpowers/specs/2026-06-10-durable-event-bus-design.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "cancel_requested_at")
