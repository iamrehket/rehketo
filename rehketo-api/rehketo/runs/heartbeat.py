from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from rehketo.db import sessionmaker

if TYPE_CHECKING:
    from uuid import UUID

HEARTBEAT_INTERVAL_SECONDS = 15.0


async def beat(run_id: UUID) -> bool:
    """Stamp heartbeat_at=now() for a running run and report whether a cancel
    is pending. Driven by a wall-clock timer independent of stream progress —
    a single LLM turn can run 30-60s producing nothing streamable, and the
    heartbeat asserts the worker still owns the run, not that the agent is
    emitting. The cancel_requested_at re-read is the backstop for a control
    NOTIFY lost while the worker's listener was mid-reconnect. Only touches
    'running' rows, so it never resurrects a row the reaper already failed."""
    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text(
                    "UPDATE runs SET heartbeat_at=now() "
                    "WHERE id=:r AND status='running' "
                    "RETURNING cancel_requested_at"
                ),
                {"r": str(run_id)},
            )
        ).one_or_none()
        await db.commit()
    return row is not None and row.cancel_requested_at is not None
