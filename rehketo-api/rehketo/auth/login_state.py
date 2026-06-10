from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert

from rehketo.db.models import PendingLogin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_pending_login(
    db: AsyncSession, *, state: str, next_path: str, ttl_seconds: int
) -> None:
    """Persist the post-login `next` path keyed by the login `state` token.

    Also prunes any already-expired rows so abandoned logins don't accumulate —
    cheap because the table only ever holds in-flight logins.
    """
    now = datetime.now(UTC)
    await db.execute(delete(PendingLogin).where(PendingLogin.expires_at < now))
    await db.execute(
        insert(PendingLogin).values(
            state=state,
            next_path=next_path,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    await db.commit()


async def consume_pending_login(db: AsyncSession, state: str) -> str | None:
    """Single-use lookup: delete the row for `state` and return its `next_path`,
    but only when the row existed and had not expired. Returns None otherwise so
    the caller falls back to the default post-login URL."""
    now = datetime.now(UTC)
    result = await db.execute(
        delete(PendingLogin)
        .where(PendingLogin.state == state)
        .returning(PendingLogin.next_path, PendingLogin.expires_at)
    )
    row = result.first()
    await db.commit()
    if row is None or row.expires_at <= now:
        return None
    # Bind to a typed local: Row attribute access is `Any`, and returning it
    # directly trips mypy's no-any-return. The annotation pins it to the
    # declared return type without an escape hatch.
    next_path: str | None = row.next_path
    return next_path
