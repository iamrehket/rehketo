"""skills: per-owner name uniqueness (owned may shadow a global)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-16 00:00:00.000000+00:00

Replaces the global UNIQUE(name) with two partial unique indexes: globals
unique among themselves, each user unique within their own set. A plain
UNIQUE(owner_user_id, name) would not enforce global uniqueness because
Postgres treats NULLs as distinct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 0014 created UniqueConstraint("name") unnamed -> Postgres default name.
    op.drop_constraint("skills_name_key", "skills", type_="unique")
    op.create_index(
        "uq_skills_global_name",
        "skills",
        ["name"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_index(
        "uq_skills_owner_name",
        "skills",
        ["owner_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_skills_owner_name", table_name="skills")
    op.drop_index("uq_skills_global_name", table_name="skills")
    # TRUNCATE discards ALL skill rows (global and owned alike) before restoring
    # the global UNIQUE constraint — mixed rows that share a name would violate
    # it and block the rollback. This path is for dev/test only; never run
    # against live data without explicit operator awareness.
    op.execute(sa.text("TRUNCATE TABLE skills"))
    op.create_unique_constraint("skills_name_key", "skills", ["name"])
