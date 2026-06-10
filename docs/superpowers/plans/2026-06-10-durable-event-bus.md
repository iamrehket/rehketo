# Durable Event Bus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the in-memory run event bus with a durable postgres-backed bus (LISTEN/NOTIFY doorbell over the existing `run_events` table), add cross-process cancellation, and give the UI reconnect/resume.

**Architecture:** `PostgresEventBus` implements the existing `RunEventBus` protocol — publish INSERTs into `run_events` + `pg_notify`; subscribe tails the table by sequence, woken by one LISTEN connection per process (with a ~5s re-poll safety net). Cancellation becomes a durable `runs.cancel_requested_at` column + NOTIFY on a control channel; the in-process `RunTaskRegistry` survives as a process-local detail. The UI auto-reconnects with `from_sequence` and resumes in-flight runs on conversation open via a new `active_run_id` field.

**Tech Stack:** FastAPI + SQLAlchemy async (psycopg3 driver), raw `psycopg.AsyncConnection` for LISTEN, alembic, pytest + testcontainers (postgres:17), SvelteKit + vitest.

**Spec:** `docs/superpowers/specs/2026-06-10-durable-event-bus-design.md`

**Prerequisites:** Docker running (integration tests spin up postgres:17 via testcontainers). All API commands run from `rehketo-api/`; all UI commands from `rehketo-ui/`.

**Conventions that apply:** every escape hatch carries a code/reason (charter 6); no AI attribution in commits; conventional commits.

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `rehketo-api/alembic/versions/0008_runs_cancel_requested_at.py` | Create | `cancel_requested_at` column |
| `rehketo-api/rehketo/db/models.py` | Modify | `Run.cancel_requested_at` |
| `rehketo-api/rehketo/runs/listen.py` | Create | reusable LISTEN loop (reconnect w/ backoff) |
| `rehketo-api/rehketo/runs/event_bus.py` | Modify | add `PostgresEventBus`; later delete `InProcessEventBus` |
| `rehketo-api/rehketo/runs/cancellation.py` | Create | `request_cancel` + `RunControlListener` |
| `rehketo-api/rehketo/agent/sweep.py` | Modify | publish closure events for swept runs |
| `rehketo-api/rehketo/main.py` | Modify | construct/start/stop bus + control listener |
| `rehketo-api/rehketo/api/runs.py` | Modify | cancel handler → `request_cancel` |
| `rehketo-api/rehketo/api/conversations.py` | Modify | `ConversationDetail.active_run_id` |
| `rehketo-ui/openapi.snapshot.json` | Rebaseline | contract snapshot |
| `rehketo-ui/src/lib/types.ts` | Modify | `active_run_id` on `ConversationDetail` |
| `rehketo-ui/src/lib/sse.ts` | Modify | auto-reconnect with `from_sequence` resume |
| `rehketo-ui/src/lib/sse.spec.ts` | Modify | reconnect test cases |
| `rehketo-ui/src/lib/components/ChatView.svelte` | Modify | resume-on-open |
| `rehketo-api/tests/integration/test_pg_listen.py` | Create | listen-loop tests |
| `rehketo-api/tests/integration/test_event_bus_postgres.py` | Create | bus contract + durability tests |
| `rehketo-api/tests/integration/test_cancellation_control.py` | Create | cross-instance cancel test |
| `rehketo-api/tests/unit/test_event_bus_contract.py` | Delete (Task 9) | superseded by postgres contract tests |

---

### Task 1: Migration — `runs.cancel_requested_at`

**Files:**
- Create: `rehketo-api/alembic/versions/0008_runs_cancel_requested_at.py`
- Modify: `rehketo-api/rehketo/db/models.py` (the `Run` model, next to `finished_at` around line 158)

- [ ] **Step 1: Write the migration**

```python
"""durable cancel request marker on runs

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-10 00:00:00.000000+00:00

Cancellation moves from an in-process-only registry call to a durable
column + NOTIFY doorbell so it works across processes (spec:
2026-06-10-durable-event-bus-design.md).
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
```

- [ ] **Step 2: Add the model field**

In `rehketo/db/models.py`, in `class Run`, directly after `finished_at`:

```python
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 3: Verify migrations apply cleanly**

Run: `uv run pytest tests/integration/test_db_fixture.py tests/unit/test_models_compile.py -v`
Expected: PASS (the `db_url` fixture runs `downgrade base` + `upgrade head`, exercising 0008 both ways).

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0008_runs_cancel_requested_at.py rehketo/db/models.py
git commit -m "feat: add runs.cancel_requested_at for durable cancellation"
```

---

### Task 2: LISTEN loop helper

One reusable coroutine used by both the event bus (events channel) and the control listener (cancel channel). It owns a dedicated raw psycopg connection (SQLAlchemy pooled connections can't sit on LISTEN), reconnects with backoff, and dispatches notification payloads to a callback.

**Files:**
- Create: `rehketo-api/rehketo/runs/listen.py`
- Test: `rehketo-api/tests/integration/test_pg_listen.py`

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import asyncio

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.listen import listen


async def _notify(channel: str, payload: str) -> None:
    async with sessionmaker()() as db:
        await db.execute(
            text("SELECT pg_notify(:chan, :payload)"),
            {"chan": channel, "payload": payload},
        )
        await db.commit()


async def test_listen_dispatches_notify_payloads(db_url: str) -> None:
    reset_engine_for_tests()
    received: asyncio.Queue[str] = asyncio.Queue()
    ready = asyncio.Event()
    task = asyncio.create_task(
        listen("test_chan", received.put_nowait, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await _notify("test_chan", "hello")
        assert await asyncio.wait_for(received.get(), timeout=10) == "hello"
    finally:
        task.cancel()


async def test_listen_ignores_other_channels(db_url: str) -> None:
    reset_engine_for_tests()
    received: asyncio.Queue[str] = asyncio.Queue()
    ready = asyncio.Event()
    task = asyncio.create_task(
        listen("chan_a", received.put_nowait, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await _notify("chan_b", "wrong")
        await _notify("chan_a", "right")
        assert await asyncio.wait_for(received.get(), timeout=10) == "right"
        assert received.empty()
    finally:
        task.cancel()
```

Note: `db_url` is the existing testcontainers fixture from `tests/conftest.py`; `reset_engine_for_tests()` makes the cached engine pick up the per-test `DATABASE_URL` (check how neighboring integration tests handle this — if e.g. `tests/integration/test_startup_sweep.py` resets the engine via a different idiom, copy that idiom instead).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_pg_listen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rehketo.runs.listen'`

- [ ] **Step 3: Implement `rehketo/runs/listen.py`**

```python
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import psycopg
from psycopg import sql

from rehketo.config import get_settings
from rehketo.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_RECONNECT_DELAY_SECONDS = 1.0


def _raw_dsn() -> str:
    """psycopg wants a plain postgresql:// DSN, not SQLAlchemy's +psycopg form."""
    return get_settings().database_url.replace("+psycopg", "", 1)


async def listen(
    channel: str,
    on_payload: Callable[[str], None],
    *,
    ready: asyncio.Event | None = None,
) -> None:
    """LISTEN on `channel` forever, dispatching each payload to `on_payload`.

    Holds a dedicated autocommit connection (LISTEN pins a connection, so it
    must not come from the pool). Reconnects with a fixed delay on any
    connection failure — subscribers degrade to their re-poll interval while
    the listener is down, so data is never lost, only delayed. Runs until
    cancelled; intended as a long-lived asyncio.Task owned by app lifespan.
    """
    while True:
        try:
            conn = await psycopg.AsyncConnection.connect(_raw_dsn(), autocommit=True)
            try:
                await conn.execute(
                    sql.SQL("LISTEN {}").format(sql.Identifier(channel))
                )
                if ready is not None:
                    ready.set()
                async for notification in conn.notifies():
                    on_payload(notification.payload)
            finally:
                await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LISTEN %s connection lost; reconnecting", channel)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_pg_listen.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add rehketo/runs/listen.py tests/integration/test_pg_listen.py
git commit -m "feat: add reusable postgres LISTEN loop"
```

---

### Task 3: PostgresEventBus

**Files:**
- Modify: `rehketo-api/rehketo/runs/event_bus.py` (add class; `InProcessEventBus` stays until Task 9)
- Test: `rehketo-api/tests/integration/test_event_bus_postgres.py`

Design notes locked by the spec:
- `publish` INSERTs the **raw** event payload; `subscribe` enriches with `sequence` and `run_id` on read (the wire shape `{**event, "sequence": seq, "run_id": run_id}` matches what `InProcessEventBus` produces today).
- Sequence is assigned in the INSERT itself: `SELECT COALESCE(MAX(sequence) + 1, 0)` scoped to the run. One publisher per run (its `run_agent` task) makes this race-free; the `(run_id, sequence)` unique constraint turns any future violation into a loud failure.
- `pg_notify` rides the same transaction as the INSERT — postgres delivers notifications on commit, so a wake can never precede its row.
- `from_sequence` is **inclusive** (matches `InProcessEventBus`: `seq >= from_sequence`).

- [ ] **Step 1: Write the failing tests**

`run_events.run_id` has an FK to `runs`, so tests need real run rows. Field names below follow `rehketo/db/models.py` — verify constructor kwargs against the actual `User`/`Conversation`/`Run` models before running.

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.db.models import Conversation, Run, User
from rehketo.runs.event_bus import PostgresEventBus


@pytest_asyncio.fixture
async def bus(db_url: str) -> AsyncIterator[PostgresEventBus]:
    reset_engine_for_tests()
    b = PostgresEventBus(poll_interval=0.2)
    await b.start()
    yield b
    await b.stop()


async def _mk_run() -> str:
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@example.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="running",
            model="test-model",
        )
        db.add_all([user, conv, run])
        await db.commit()
        return str(run.id)


async def _collect(
    bus: PostgresEventBus,
    run_id: str,
    n: int,
    *,
    from_sequence: int | None = None,
) -> list[dict]:
    events: list[dict] = []
    async for e in bus.subscribe(run_id, from_sequence=from_sequence):
        events.append(e)
        if len(events) >= n:
            break
    return events


async def test_publish_then_subscribe_replays(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()
    for i in range(5):
        await bus.publish(run_id, {"type": "tick", "i": i})
    events = await asyncio.wait_for(_collect(bus, run_id, 5), timeout=10)
    assert [e["i"] for e in events] == [0, 1, 2, 3, 4]
    assert [e["sequence"] for e in events] == [0, 1, 2, 3, 4]
    assert all(e["run_id"] == run_id for e in events)


async def test_live_publish_wakes_subscriber(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()

    async def publisher() -> None:
        await asyncio.sleep(0.1)
        for i in range(3):
            await bus.publish(run_id, {"type": "tick", "i": i})

    task = asyncio.create_task(publisher())
    events = await asyncio.wait_for(_collect(bus, run_id, 3), timeout=10)
    await task
    assert [e["i"] for e in events] == [0, 1, 2]


async def test_from_sequence_resumes_inclusive(bus: PostgresEventBus) -> None:
    run_id = await _mk_run()
    for i in range(5):
        await bus.publish(run_id, {"type": "tick", "i": i})
    events = await asyncio.wait_for(
        _collect(bus, run_id, 2, from_sequence=3), timeout=10
    )
    assert [e["i"] for e in events] == [3, 4]


async def test_isolation_between_runs(bus: PostgresEventBus) -> None:
    r1, r2 = await _mk_run(), await _mk_run()
    await bus.publish(r1, {"type": "tick"})
    await bus.publish(r2, {"type": "tock"})
    e1 = await asyncio.wait_for(_collect(bus, r1, 1), timeout=10)
    e2 = await asyncio.wait_for(_collect(bus, r2, 1), timeout=10)
    assert e1[0]["type"] == "tick"
    assert e2[0]["type"] == "tock"


async def test_cross_instance_delivery(bus: PostgresEventBus, db_url: str) -> None:
    """Publish through one bus instance, receive through another — proves
    NOTIFY wiring between independent listener connections, the multi-process
    case in miniature."""
    other = PostgresEventBus(poll_interval=30.0)  # long poll: NOTIFY must do it
    await other.start()
    try:
        run_id = await _mk_run()
        collector = asyncio.create_task(_collect(other, run_id, 2))
        await asyncio.sleep(0.1)
        await bus.publish(run_id, {"type": "a"})
        await bus.publish(run_id, {"type": "b"})
        events = await asyncio.wait_for(collector, timeout=10)
        assert [e["type"] for e in events] == ["a", "b"]
    finally:
        await other.stop()


async def test_events_survive_bus_restart(bus: PostgresEventBus) -> None:
    """The durability claim itself: a fresh instance replays everything."""
    run_id = await _mk_run()
    await bus.publish(run_id, {"type": "tick", "i": 0})
    await bus.publish(run_id, {"type": "run.ended"})
    await bus.stop()

    fresh = PostgresEventBus(poll_interval=0.2)
    await fresh.start()
    try:
        events = await asyncio.wait_for(_collect(fresh, run_id, 2), timeout=10)
        assert [e["type"] for e in events] == ["tick", "run.ended"]
    finally:
        await fresh.stop()
        await bus.start()  # leave the fixture stoppable
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_event_bus_postgres.py -v`
Expected: FAIL with `ImportError: cannot import name 'PostgresEventBus'`

- [ ] **Step 3: Implement `PostgresEventBus`**

Add to `rehketo/runs/event_bus.py` (new imports at top: `contextlib`, `json`, `from sqlalchemy import text`, `from rehketo.db import sessionmaker`, `from rehketo.runs.listen import listen`):

```python
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
```

Type-checker notes: `subscribe` is an async generator method, which satisfies the protocol's `AsyncIterator` return. If mypy complains about the `defaultdict` import or `AsyncIterator` being TYPE_CHECKING-only, move imports accordingly — `defaultdict` is already imported in this module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_event_bus_postgres.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Run quality gates**

Run: `uv run ruff format --check && uv run ruff check && uv run mypy rehketo`
Expected: clean (fix any findings before committing)

- [ ] **Step 6: Commit**

```bash
git add rehketo/runs/event_bus.py tests/integration/test_event_bus_postgres.py
git commit -m "feat: add durable PostgresEventBus over run_events"
```

---

### Task 4: Cut the app over to PostgresEventBus

**Files:**
- Modify: `rehketo-api/rehketo/main.py` (bus construction at line ~108, lifespan at ~83)

- [ ] **Step 1: Swap construction and add lifecycle**

In `main.py`, replace the import `from rehketo.runs.event_bus import InProcessEventBus` with `from rehketo.runs.event_bus import PostgresEventBus`, and in `create_app()` replace:

```python
    app.state.event_bus = InProcessEventBus()
```

with:

```python
    # Constructed here (no I/O); its LISTEN task starts in _lifespan.
    app.state.event_bus = PostgresEventBus()
```

Replace `_lifespan` with:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    logger.info("rehketo-api starting app_env=%s", settings.app_env)
    await app.state.event_bus.start()
    await sweep_abandoned_runs()
    try:
        yield
    finally:
        await app.state.event_bus.stop()
```

(`sweep_abandoned_runs` gains a bus argument in Task 5 — leave the call as-is for now.)

- [ ] **Step 2: Run the integration suite**

Run: `uv run pytest tests/integration -v`
Expected: PASS. Every test that exercises chat/SSE now runs against the postgres bus. If any test constructed `InProcessEventBus` directly or assumed in-memory behavior, fix it here — the SSE wire contract is unchanged, so failures should be wiring-shaped (e.g. lifespan not running), not contract-shaped.

- [ ] **Step 3: Run e2e suite**

Run: `uv run pytest tests/e2e -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add rehketo/main.py
git commit -m "feat: cut app over to durable postgres event bus"
```

---

### Task 5: Startup sweep publishes closure events

**Files:**
- Modify: `rehketo-api/rehketo/agent/sweep.py`
- Modify: `rehketo-api/rehketo/main.py` (pass the bus)
- Test: extend `rehketo-api/tests/integration/test_startup_sweep.py`

- [ ] **Step 1: Write the failing test**

Read `tests/integration/test_startup_sweep.py` first — if it already has a helper that creates a `running` run, use that instead of the inline block below. Add:

```python
async def _mk_running_run() -> str:
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@example.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="running",
            model="test-model",
        )
        db.add_all([user, conv, run])
        await db.commit()
        return str(run.id)


async def test_sweep_publishes_closure_events(db_url: str) -> None:
    """A client reconnecting to a dead run's stream must get the normal
    terminal sequence (run.status=failed + run.ended), not a hang."""
    reset_engine_for_tests()
    run_id = await _mk_running_run()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await sweep_abandoned_runs(bus)

        events = []
        async for e in bus.subscribe(str(run_id)):
            events.append(e)
            if e["type"] == "run.ended":
                break
        statuses = [e for e in events if e["type"] == "run.status"]
        assert statuses[-1]["status"] == "failed"
        assert statuses[-1]["error"]["code"] == "process_restart"
        assert events[-1]["type"] == "run.ended"
    finally:
        await bus.stop()
```

Wrap the subscribe loop in `asyncio.wait_for(..., timeout=10)` like the Task 3 tests.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_startup_sweep.py -v`
Expected: New test FAILS with `TypeError: sweep_abandoned_runs() takes 0 positional arguments but 1 was given`

- [ ] **Step 3: Implement**

In `rehketo/agent/sweep.py` — change the signature and publish closure per swept run:

```python
async def sweep_abandoned_runs(bus: RunEventBus) -> None:
    """On startup, mark any runs stuck in `running` or `queued` as failed,
    and publish the terminal event pair so any client still subscribed to a
    dead run's stream gets a clean close instead of a hang.

    Anything in those states at startup was abandoned by the previous
    process; the checkpointer may still have state but we do not resume yet
    (that arrives with the agent worker split).
    """
    error = {
        "code": "process_restart",
        "message": "run abandoned by process restart",
    }
    async with sessionmaker()() as db:
        result = await db.execute(
            update(Run)
            .where(Run.status.in_(["queued", "running"]))
            .values(
                status="failed",
                error=error,
                finished_at=datetime.now(UTC),
            )
            .returning(Run.id)
        )
        ids = [row[0] for row in result.all()]
        await db.commit()
    for run_id in ids:
        await bus.publish(
            str(run_id), {"type": "run.status", "status": "failed", "error": error}
        )
        await bus.publish(str(run_id), {"type": "run.ended"})
    if ids:
        logger.info("swept %d abandoned runs on startup", len(ids))
```

Import `RunEventBus` under `TYPE_CHECKING` from `rehketo.runs.event_bus`.

In `main.py` `_lifespan`, change the call to:

```python
    await sweep_abandoned_runs(app.state.event_bus)
```

Update any existing direct callers in `tests/integration/test_startup_sweep.py` to pass a started `PostgresEventBus` (or construct one per the new test).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_startup_sweep.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Commit**

```bash
git add rehketo/agent/sweep.py rehketo/main.py tests/integration/test_startup_sweep.py
git commit -m "feat: sweep publishes terminal events for abandoned runs"
```

---

### Task 6: Cross-process cancellation

**Files:**
- Create: `rehketo-api/rehketo/runs/cancellation.py`
- Modify: `rehketo-api/rehketo/api/runs.py` (cancel handler, lines ~108-131)
- Modify: `rehketo-api/rehketo/main.py` (start/stop control listener)
- Test: `rehketo-api/tests/integration/test_cancellation_control.py`
- Modify: existing cancel tests (`test_run_cancel.py`, `test_run_cancel_shield.py`, `test_run_cancel_terminal.py`) — propagation is now async

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import asyncio
from uuid import UUID

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.cancellation import RunControlListener, request_cancel
from rehketo.runs.registry import RunTaskRegistry


async def test_cancel_reaches_task_via_control_channel(db_url: str) -> None:
    """The cancel request travels: DB column + NOTIFY -> listener -> registry
    -> task.cancel(). The requester shares no memory with the task holder —
    this is the cross-process path in miniature."""
    reset_engine_for_tests()
    run_id = await _mk_running_run()  # same helper as Task 5's test —
    # repeat it verbatim in this file (User/Conversation/Run rows with
    # status='running'); test modules don't import from each other.

    registry = RunTaskRegistry()
    cancelled = asyncio.Event()

    async def fake_run() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(fake_run())
    registry.register(UUID(run_id), task)

    listener = RunControlListener(registry)
    await listener.start()
    try:
        async with sessionmaker()() as db:
            await request_cancel(db, UUID(run_id))
        await asyncio.wait_for(cancelled.wait(), timeout=10)
    finally:
        await listener.stop()

    # The durable record exists regardless of delivery.
    async with sessionmaker()() as db:
        from sqlalchemy import select

        from rehketo.db.models import Run

        run = (
            await db.execute(select(Run).where(Run.id == UUID(run_id)))
        ).scalar_one()
        assert run.cancel_requested_at is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_cancellation_control.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rehketo.runs.cancellation'`

- [ ] **Step 3: Implement `rehketo/runs/cancellation.py`**

```python
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text, update

from rehketo.core.logging import get_logger
from rehketo.db.models import Run
from rehketo.runs.listen import listen

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from rehketo.runs.registry import RunTaskRegistry

logger = get_logger(__name__)

CONTROL_CHANNEL = "run_control"


async def request_cancel(db: AsyncSession, run_id: UUID) -> None:
    """Record the cancel durably, then ring the doorbell. The column is the
    source of truth; NOTIFY is the optimization — same pattern as the event
    bus. Whichever process holds the run's task reacts; if none does, the
    run already died and the startup sweep owns its closure."""
    await db.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(cancel_requested_at=datetime.now(UTC))
    )
    await db.execute(
        text("SELECT pg_notify(:chan, :rid)"),
        {"chan": CONTROL_CHANNEL, "rid": str(run_id)},
    )
    await db.commit()


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
```

- [ ] **Step 4: Rewire the cancel handler**

In `rehketo/api/runs.py`, replace the body after the 409 check in `cancel_run`:

```python
    if run.status in _TERMINAL_RUN_STATES:
        raise HTTPException(status_code=409, detail=f"run already {run.status}")
    await request_cancel(db, run_id)
```

Remove the `registry = request.app.state.task_registry` / `registry.cancel(run_id)` lines and the now-unused `request: Request` parameter if nothing else in the handler uses it (charter 8: no orphan code). Import `request_cancel` from `rehketo.runs.cancellation`.

- [ ] **Step 5: Wire the listener into lifespan**

In `main.py` `create_app()`, after the bus line:

```python
    app.state.control_listener = RunControlListener(app.state.task_registry)
```

In `_lifespan`, start it after the bus and stop it in the `finally`:

```python
    await app.state.event_bus.start()
    await app.state.control_listener.start()
    await sweep_abandoned_runs(app.state.event_bus)
    try:
        yield
    finally:
        await app.state.control_listener.stop()
        await app.state.event_bus.stop()
```

- [ ] **Step 6: Adapt existing cancel tests**

`POST /runs/{id}/cancel` no longer cancels synchronously — it commits + notifies, and the listener cancels the task a beat later. Read `test_run_cancel.py`, `test_run_cancel_shield.py`, `test_run_cancel_terminal.py`; wherever they assert immediate post-POST effects, wait for propagation instead, e.g.:

```python
# before: resp = client.post(...); assert task.cancelled()
# after:
resp = await client.post(f"/runs/{run_id}/cancel")
assert resp.status_code == 204
await asyncio.wait_for(task, timeout=10)  # or poll run.status until 'cancelled'
```

`test_run_cancel_terminal.py` (the 409 path) should pass unchanged.

- [ ] **Step 7: Run the suites**

Run: `uv run pytest tests/integration/test_cancellation_control.py tests/integration/test_run_cancel.py tests/integration/test_run_cancel_shield.py tests/integration/test_run_cancel_terminal.py -v`
Expected: PASS

Run: `uv run pytest && uv run mypy rehketo && uv run ruff check`
Expected: PASS / clean

- [ ] **Step 8: Commit**

```bash
git add rehketo/runs/cancellation.py rehketo/api/runs.py rehketo/main.py tests/integration/
git commit -m "feat: cross-process run cancellation via control channel"
```

---

### Task 7: `active_run_id` on ConversationDetail

The UI cannot resume what it cannot see: GET `/conversations/{id}` must expose an in-flight run.

**Files:**
- Modify: `rehketo-api/rehketo/api/conversations.py` (`ConversationDetail` model + `get_conversation`)
- Modify: `rehketo-ui/openapi.snapshot.json` (rebaseline)
- Modify: `rehketo-ui/src/lib/types.ts`
- Test: extend `rehketo-api/tests/integration/test_conversations_detail.py`

- [ ] **Step 1: Write the failing tests**

Read `test_conversations_detail.py` and follow its client/fixture idiom. Add two cases:

```python
async def test_detail_exposes_active_run_id(...) -> None:
    # create conversation; insert a Run with status='running' linked to it
    # GET /conversations/{id}
    assert body["active_run_id"] == str(run_id)


async def test_detail_active_run_id_null_when_terminal(...) -> None:
    # create conversation; insert a Run with status='succeeded'
    # GET /conversations/{id}
    assert body["active_run_id"] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_conversations_detail.py -v`
Expected: New tests FAIL with `KeyError: 'active_run_id'`

- [ ] **Step 3: Implement**

In `conversations.py`, add to `ConversationDetail`:

```python
class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
    # In-flight run for this conversation (queued/running), newest first.
    # The UI uses this to reattach to the live SSE stream on open.
    active_run_id: UUID | None = None
```

In `get_conversation`, before the `return`:

```python
    active_run_id = (
        await db.execute(
            select(Run.id)
            .where(
                Run.conversation_id == conv.id,
                Run.status.in_(["queued", "running"]),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
```

and pass `active_run_id=active_run_id` to the `ConversationDetail(...)` constructor.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/test_conversations_detail.py -v`
Expected: PASS

- [ ] **Step 5: Rebaseline the contract snapshot and update UI types**

Run: `uv run python ../tools/check_contract.py --update`
Then in `rehketo-ui/src/lib/types.ts`:

```typescript
export type ConversationDetail = ConversationSummary & {
	messages: MessageOut[];
	active_run_id: string | null;
};
```

Run: `uv run python ../tools/check_contract.py`
Expected: `OK` (no drift)

- [ ] **Step 6: Commit**

```bash
git add rehketo/api/conversations.py tests/integration/test_conversations_detail.py ../rehketo-ui/openapi.snapshot.json ../rehketo-ui/src/lib/types.ts
git commit -m "feat: expose active_run_id on conversation detail"
```

---

### Task 8: UI — auto-reconnect with resume in `sse.ts`

All reconnect logic lives in `sse.ts` so it's unit-testable; `ChatView` stays a thin consumer. Behavior: track the highest `sequence` seen; on a mid-run connection error, reconnect with `from_sequence = last + 1` (inclusive semantics ⇒ no duplicates), exponential backoff 500ms/1s/2s, 3 attempts, budget reset by any received event. `onError` now fires only when attempts are exhausted — the existing "Disconnected" banner semantics in ChatView stay correct.

**Files:**
- Modify: `rehketo-ui/src/lib/sse.ts`
- Test: `rehketo-ui/src/lib/sse.spec.ts`

- [ ] **Step 1: Write the failing tests**

Read `sse.spec.ts` first — it has a mock `EventSource` injected via `opts.EventSourceImpl`; reuse it. Use vitest fake timers for backoff. Add cases:

```typescript
describe('reconnect with resume', () => {
	it('reconnects after a mid-run error with from_sequence = last seen + 1', () => {
		// subscribe; deliver message.delta with sequence 0 and 1; fire error.
		// advance timers past 500ms.
		// assert: a second EventSource was constructed with URL containing
		// 'from_sequence=2'; onError NOT called.
	});

	it('does not reconnect after run.ended', () => {
		// deliver run.ended; fire error on the (closed) source.
		// assert: exactly one EventSource ever constructed.
	});

	it('does not reconnect after a terminal run.status', () => {
		// deliver run.status succeeded; fire error.
		// assert: one EventSource; onEnded called (existing EOF-tail behavior).
	});

	it('surfaces onError after exhausting reconnect attempts', () => {
		// fire error on each successive source without delivering events,
		// advancing timers (500, 1000, 2000ms).
		// assert: 4 sources constructed (1 + 3 retries), then onError called
		// and state === 'closed'.
	});

	it('received events reset the attempt budget', () => {
		// error -> reconnect -> deliver a delta (sequence 5) -> error again.
		// assert: next reconnect uses from_sequence=6 and the budget restarted
		// (a further 3 attempts available).
	});

	it('unsubscribe during backoff cancels the pending reconnect', () => {
		// fire error; call unsubscribe(); advance timers far.
		// assert: no second EventSource constructed.
	});
});
```

Write these as real tests against the existing mock — the comments above describe the assertions, the spec file's existing helpers show the mechanics.

- [ ] **Step 2: Run to verify they fail**

Run: `pnpm run test:unit -- --run`
Expected: new tests FAIL (no reconnection occurs; only one EventSource is ever constructed)

- [ ] **Step 3: Implement in `sse.ts`**

Restructure `subscribeRun` — extract connection setup into an inner `connect()`, add sequence/attempt tracking. Complete new body (types, helpers, and handler signatures unchanged from the current file):

```typescript
const MAX_RECONNECT_ATTEMPTS = 3;
const BASE_RETRY_MS = 500;

export function subscribeRun(
	runId: string,
	handlers: RunStreamHandlers,
	opts: { fromSequence?: number; EventSourceImpl?: EventSourceCtor } = {}
): RunStreamSubscription {
	const Ctor = opts.EventSourceImpl ?? (globalThis.EventSource as EventSourceCtor);

	const sub: { state: StreamState } = { state: 'idle' };
	let closed = false;
	let source: EventSource | null = null;
	let retryTimer: ReturnType<typeof setTimeout> | null = null;
	// Highest sequence seen; reconnects resume at last + 1 (from_sequence is
	// inclusive on the server). Starts one below the caller's fromSequence so
	// a pre-first-event reconnect re-requests the same window.
	let lastSequence = opts.fromSequence !== undefined ? opts.fromSequence - 1 : -1;
	let attempts = 0;

	function buildUrl(fromSequence?: number): string {
		const params = new URLSearchParams();
		if (fromSequence !== undefined && fromSequence >= 0) {
			params.set('from_sequence', String(fromSequence));
		}
		const qs = params.toString();
		return `/runs/${encodeURIComponent(runId)}/events${qs ? `?${qs}` : ''}`;
	}

	function close(final: StreamState): void {
		if (closed) return;
		closed = true;
		sub.state = final;
		source?.close();
		handlers.onEnded?.();
	}

	function parseOrError<E extends RunEvent>(evt: Event): E | null {
		const data = (evt as MessageEvent<string>).data;
		try {
			return JSON.parse(data) as E;
		} catch (err) {
			handlers.onError?.(err);
			return null;
		}
	}

	// Every delivered event proves the connection works: record progress and
	// refill the reconnect budget.
	function track(event: { sequence: number }): void {
		lastSequence = event.sequence;
		attempts = 0;
	}

	function connect(fromSequence?: number): void {
		source = new Ctor(buildUrl(fromSequence), { withCredentials: true });

		source.addEventListener('message.delta', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'message.delta' }>>(evt);
			if (!event) return;
			track(event);
			if (sub.state === 'idle' || sub.state === 'queued') sub.state = 'running';
			handlers.onDelta?.(event.delta, event);
		});

		source.addEventListener('message.complete', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'message.complete' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onMessageComplete?.(event.message);
		});

		source.addEventListener('conversation.updated', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'conversation.updated' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onConversationUpdated?.(event.conversation_id, event.title);
		});

		source.addEventListener('run.status', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'run.status' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onStatus?.(event.status, event.error);
			if (event.status === 'queued') {
				if (sub.state === 'idle') sub.state = 'queued';
			} else if (event.status === 'running') {
				sub.state = 'running';
			} else if (event.status === 'succeeded') {
				sub.state = 'terminalSucceeded';
			} else if (event.status === 'failed') {
				sub.state = 'terminalFailed';
			} else if (event.status === 'cancelled') {
				sub.state = 'terminalCancelled';
			}
		});

		source.addEventListener('run.ended', () => {
			close(isTerminal(sub.state) ? sub.state : 'closed');
		});

		source.addEventListener('error', (err) => {
			// Normal EOF tail after a terminal status — close quietly (the
			// server closes the HTTP stream after run.ended).
			if (closed) return;
			if (isTerminal(sub.state)) {
				close(sub.state);
				return;
			}
			// Mid-run drop: the bus is durable, so resume from the next
			// sequence instead of surfacing a disconnect.
			source?.close();
			if (attempts < MAX_RECONNECT_ATTEMPTS) {
				const delay = BASE_RETRY_MS * 2 ** attempts;
				attempts += 1;
				retryTimer = setTimeout(() => {
					retryTimer = null;
					connect(lastSequence + 1);
				}, delay);
				return;
			}
			handlers.onError?.(err);
			close('closed');
		});
	}

	connect(opts.fromSequence);

	return {
		get state(): StreamState {
			return sub.state;
		},
		unsubscribe(): void {
			if (closed) return;
			closed = true;
			if (retryTimer !== null) clearTimeout(retryTimer);
			source?.close();
		}
	};
}
```

Update the module header comment to document the reconnect behavior (replace the stale "closes on error" description).

- [ ] **Step 4: Run to verify they pass**

Run: `pnpm run test:unit -- --run`
Expected: PASS (new and pre-existing sse cases; some pre-existing error-path tests may need updating to account for retries — e.g. a test asserting `onError` after a single error must now exhaust the budget first. Adjust those tests' setups, not the behavior.)

- [ ] **Step 5: Lint and typecheck**

Run: `pnpm run lint && pnpm run check`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/lib/sse.ts src/lib/sse.spec.ts
git commit -m "feat: auto-reconnect SSE streams with sequence resume"
```

---

### Task 9: UI — resume on conversation open, and cleanup

**Files:**
- Modify: `rehketo-ui/src/lib/components/ChatView.svelte`
- Modify: `rehketo-api/rehketo/runs/event_bus.py` (delete `InProcessEventBus`)
- Delete: `rehketo-api/tests/unit/test_event_bus_contract.py`

- [ ] **Step 1: Resume on open in ChatView**

In `ChatView.svelte`, after the `attachRun` function definition, add a one-time init (the component is recreated per conversation, so a plain statement runs once per open):

```typescript
	// Reattach to an in-flight run on open: replay from sequence 0 rebuilds
	// the streaming bubble, then live events continue. This is what makes the
	// durable bus visible — start a run on one device, watch it on another.
	if (conversation.active_run_id) {
		attachRun(conversation.active_run_id);
	}
```

`attachRun` already initializes `streamingText = ''` and subscribes from the start (no `fromSequence` ⇒ full replay), so no further change is needed.

- [ ] **Step 2: Verify UI suite still green**

Run: `pnpm run lint && pnpm run check && pnpm run test:unit -- --run`
Expected: clean / PASS

- [ ] **Step 3: Delete `InProcessEventBus`**

In `rehketo/runs/event_bus.py`: delete the `InProcessEventBus` class and any imports it alone used (`deque`, possibly `cast`). Keep the `RunEventBus` protocol and `PostgresEventBus`. Delete `tests/unit/test_event_bus_contract.py` (its coverage lives in `tests/integration/test_event_bus_postgres.py`).

Run: `grep -rn "InProcessEventBus" rehketo tests ../tools ../rehketo-ui/src`
Expected: no hits.

- [ ] **Step 4: Full validation — quote real output**

From `rehketo-api/`:
```bash
uv run ruff format --check && uv run ruff check && uv run mypy rehketo \
  && uv run bandit -r rehketo && uv run lint-imports && uv run pytest \
  && uv run python ../tools/check_contract.py
```
From `rehketo-ui/`:
```bash
pnpm run lint && pnpm run check && pnpm run test:unit -- --run
```
From repo root:
```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```
Expected: all PASS — quote the output in the completion report (charter 5).

- [ ] **Step 5: Manual validation (from the spec)**

With `just db` + `just api` + `just ui` running:
1. Start a run; kill the API mid-stream; restart it; reload the page → the conversation shows the swept `failed` state cleanly (no hang, no zombie spinner).
2. Start a long run in one tab; open the same conversation in a second tab → the live stream appears (replay + live tail).
3. Start a run; cancel it → the bubble lands with a `cancelled` badge (cross-process path: cancel now travels via NOTIFY even single-process).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: resume in-flight runs on conversation open; drop in-process bus"
```

---

## Post-plan note

`docs/superpowers/specs/2026-04-19-chat-and-agent-v1-design.md` §6.2 describes this cutover as a fast-follow; no doc update needed there (the roadmap doc already records the scope decision). The `runs(status)` partial index from the v1 schema serves the sweep and `active_run_id` queries.
