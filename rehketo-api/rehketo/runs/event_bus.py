from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Protocol, cast

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


class InProcessEventBus:
    """asyncio.Queue-per-run bus with a bounded ring buffer for late subscribers.

    Single-process only. The postgres LISTEN/NOTIFY implementation (fast-follow)
    will be a drop-in replacement satisfying the same contract.
    """

    def __init__(self, *, buffer_size: int = 1024) -> None:
        self._buffer_size = buffer_size
        self._seq: dict[str, int] = defaultdict(int)
        self._history: dict[str, deque[dict[str, object]]] = defaultdict(
            lambda: deque(maxlen=self._buffer_size)
        )
        self._queues: dict[str, list[asyncio.Queue[dict[str, object]]]] = defaultdict(
            list
        )
        self._lock = asyncio.Lock()

    async def publish(self, run_id: str, event: dict[str, object]) -> None:
        async with self._lock:
            seq = self._seq[run_id]
            self._seq[run_id] = seq + 1
            enriched = {**event, "sequence": seq, "run_id": run_id}
            self._history[run_id].append(enriched)
            for q in list(self._queues[run_id]):
                q.put_nowait(enriched)

    async def subscribe(
        self,
        run_id: str,
        *,
        from_sequence: int | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        q: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        async with self._lock:
            # Replay buffered history from from_sequence onward
            if self._history.get(run_id):
                for e in self._history[run_id]:
                    seq = cast("int", e["sequence"])
                    if from_sequence is None or seq >= from_sequence:
                        q.put_nowait(e)
            self._queues[run_id].append(q)
        try:
            while True:
                event = await q.get()
                yield event
        finally:
            async with self._lock:
                if q in self._queues[run_id]:
                    self._queues[run_id].remove(q)


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

    async def start(self) -> None:
        ready = asyncio.Event()
        self._listener = asyncio.create_task(
            listen(EVENTS_CHANNEL, self._on_notify, ready=ready)
        )
        # Deliberately unbounded: startup blocks until postgres accepts LISTEN —
        # the app is useless without the DB, and the sweep right after would
        # fail anyway.
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
        async with sessionmaker()() as db:
            # Sequence assigned in the INSERT: safe because each run has
            # exactly one publisher (its run_agent task); the (run_id,
            # sequence) unique constraint makes any violation loud.
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
