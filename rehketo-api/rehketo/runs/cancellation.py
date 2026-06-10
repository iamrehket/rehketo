from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import text, update

from rehketo.core.logging import get_logger
from rehketo.db.models import Run
from rehketo.runs.listen import listen

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

    from rehketo.runs.registry import RunTaskRegistry

logger = get_logger(__name__)

CONTROL_CHANNEL = "run_control"
TERMINAL_RUN_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


async def request_cancel(db: AsyncSession, run_id: UUID) -> bool:
    """Record the cancel durably, then ring the doorbell. The column is the
    source of truth; NOTIFY is the optimization — same pattern as the event
    bus. Whichever process holds the run's task reacts; if none does, the
    run already died and the startup sweep closes it (as failed).

    The terminal guard lives in the UPDATE itself so a run finishing
    concurrently can never be stamped: returns False (and notifies nothing)
    when the run was already terminal.

    Accepted gap: nothing re-reads the column today, so a NOTIFY that fires
    while the owning process's control listener is mid-reconnect is lost —
    recovery is the user cancelling again (a second request re-stamps and
    re-notifies). The agent-worker milestone consumes the column properly
    (claimed runs poll it), which closes the window."""
    result = cast(
        "CursorResult[tuple[()]]",
        await db.execute(
            update(Run)
            .where(Run.id == run_id, Run.status.notin_(TERMINAL_RUN_STATES))
            .values(cancel_requested_at=datetime.now(UTC))
        ),
    )
    if (result.rowcount or 0) == 0:
        await db.rollback()
        return False
    await db.execute(
        text("SELECT pg_notify(:chan, :rid)"),
        {"chan": CONTROL_CHANNEL, "rid": str(run_id)},
    )
    await db.commit()
    return True


class RunControlListener:
    """Per-process LISTEN on the control channel; cancels local tasks. Owned
    by app lifespan, like the event bus listener."""

    def __init__(self, registry: RunTaskRegistry) -> None:
        self._registry = registry
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        ready = asyncio.Event()
        self._task = asyncio.create_task(
            listen(CONTROL_CHANNEL, self._on_notify, ready=ready)
        )
        await ready.wait()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _on_notify(self, payload: str) -> None:
        try:
            run_id = UUID(payload)
        except ValueError:
            logger.warning("ignoring malformed run_control payload: %r", payload)
            return
        if self._registry.cancel(run_id):
            logger.info("cancelled run %s via control channel", run_id)
