from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from rehketo.db import sessionmaker
from rehketo.runs.listen import listen

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class RunEventBus(Protocol):
    async def publish(self, run_id: str, event: dict[str, object]) -> None: ...
    def subscribe(
        self,
        run_id: str,
        *,
        from_sequence: int | None = None,
    ) -> AsyncIterator[dict[str, object]]: ...


EVENTS_CHANNEL = "run_events"


class PostgresEventBus:
    """Durable bus over the run_events table.

    NOTIFY is a doorbell, never a payload carrier (postgres caps payloads at
    ~8KB; deltas can exceed it). The table is the source of truth: publish
    INSERTs + notifies in one transaction, subscribe tails by sequence and is
    woken by the per-process listener, with a periodic re-poll so a missed
    notification costs latency, not data.
    """

    def __init__(self, *, poll_interval: float = 5.0) -> None:
        self._poll_interval = poll_interval
        self._wakes: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._listener: asyncio.Task[None] | None = None
        # publish() computes MAX(sequence)+1 per run; concurrent publishers
        # for the same run (parallel tool calls + the delta stream loop) race
        # that read. All of a run's publishers live in this process — true
        # today and after the M4 worker split — so a process-local per-run
        # lock is sufficient. Popped when run.ended is published, success or
        # failure.
        self._publish_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        ready = asyncio.Event()
        self._listener = asyncio.create_task(
            listen(EVENTS_CHANNEL, self._on_notify, ready=ready)
        )
        # Deliberately unbounded: startup blocks until postgres accepts LISTEN —
        # the app/worker is useless without the DB.
        await ready.wait()

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None

    def _on_notify(self, run_id: str) -> None:
        for wake in self._wakes.get(run_id, ()):
            wake.set()

    async def publish(self, run_id: str, event: dict[str, object]) -> None:
        lock = self._publish_locks.setdefault(run_id, asyncio.Lock())
        try:
            async with lock, sessionmaker()() as db:
                # Sequence assigned in the INSERT; the per-run lock above
                # serializes concurrent publishers (parallel tool calls), and
                # the (run_id, sequence) unique constraint makes any violation
                # loud.
                await db.execute(
                    text(
                        "INSERT INTO run_events (run_id, sequence, payload) "
                        "SELECT :rid, COALESCE(MAX(sequence) + 1, 0), "
                        "CAST(:payload AS jsonb) "
                        "FROM run_events WHERE run_id = :rid"
                    ),
                    {"rid": run_id, "payload": json.dumps(event, default=str)},
                )
                # Same transaction as the INSERT — postgres delivers NOTIFY on
                # commit, so a wake can never precede its row.
                await db.execute(
                    text("SELECT pg_notify(:chan, :rid)"),
                    {"chan": EVENTS_CHANNEL, "rid": run_id},
                )
                await db.commit()
        finally:
            if event.get("type") == "run.ended":
                self._publish_locks.pop(run_id, None)

    async def subscribe(
        self,
        run_id: str,
        *,
        from_sequence: int | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        wake = asyncio.Event()
        self._wakes[run_id].add(wake)
        try:
            last = (from_sequence if from_sequence is not None else 0) - 1
            while True:
                # Clear BEFORE fetching: a notify landing during the fetch
                # re-arms the wake instead of being lost.
                wake.clear()
                for seq, payload in await self._fetch_after(run_id, last):
                    last = seq
                    yield {**payload, "sequence": seq, "run_id": run_id}
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=self._poll_interval)
        finally:
            self._wakes[run_id].discard(wake)
            if not self._wakes[run_id]:
                del self._wakes[run_id]

    async def _fetch_after(
        self, run_id: str, last: int
    ) -> list[tuple[int, dict[str, object]]]:
        async def _query() -> list[tuple[int, dict[str, object]]]:
            async with sessionmaker()() as db:
                rows = await db.execute(
                    text(
                        "SELECT sequence, payload FROM run_events "
                        "WHERE run_id = :rid AND sequence > :last "
                        "ORDER BY sequence"
                    ),
                    {"rid": run_id, "last": last},
                )
                return [(row.sequence, row.payload) for row in rows]

        # Shielded so a client disconnect (CancelledError thrown into the
        # generator mid-fetch) can't cancel the session's cleanup and orphan
        # an idle-in-transaction connection; the query is short, so letting
        # it finish before the cancellation propagates is cheap.
        return await asyncio.shield(_query())
