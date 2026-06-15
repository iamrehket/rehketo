# Agent Worker Split (M4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move agent run execution out of the API process into a dedicated, restart-safe worker process that claims runs from the `runs` table, with clean-boundary resume and pool-safe liveness detection.

**Architecture:** The API only inserts `queued` runs and `NOTIFY`s a doorbell; a worker process claims runs via `SELECT … FOR UPDATE SKIP LOCKED`, drives `run_agent`, heartbeats the row, and runs a reaper that fails runs whose owning worker died. Approval pauses release the worker (`pending_approval` becomes a parked, re-claimable state); the bus and SSE/UI contracts are unchanged.

**Tech Stack:** FastAPI, SQLAlchemy async + psycopg3, Alembic, LangGraph (postgres checkpointer), postgres `LISTEN/NOTIFY`, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-06-14-agent-worker-split-design.md`

---

## File structure

New files:
- `rehketo/runs/claim.py` — `claim_next_run`, `notify_run_queued`, `RUN_QUEUED_CHANNEL`, `ClaimedRun`.
- `rehketo/runs/reaper.py` — `reap_stale_runs`, `run_reaper`, `REAP_THRESHOLD_SECONDS`, `REAP_INTERVAL_SECONDS`.
- `rehketo/runs/heartbeat.py` — `beat`, `HEARTBEAT_INTERVAL_SECONDS`.
- `rehketo/runs/worker.py` — `run_worker` (claim loop + per-run supervision + reaper wiring).
- `rehketo/cli/worker.py` — worker process entrypoint.
- `alembic/versions/0013_runs_heartbeat_at.py` — `heartbeat_at` column + claim/reaper indexes.

Modified files:
- `rehketo/db/models.py` — add `Run.heartbeat_at`.
- `rehketo/api/messages.py` — stop spawning a task; INSERT `queued` + `notify_run_queued`.
- `rehketo/main.py` — drop the startup sweep, the API-side task registry and control listener.
- `rehketo/agent/run.py` — release-on-interrupt; resume detection; segment rehydration; `run.ended` only on terminal outcomes.
- `rehketo/agent/approval.py` — `park_on_interrupt` + `build_resume_command`; delete `wait_for_decisions` and `resolve_interrupt`.
- `rehketo/api/runs.py` — `decide_approval` flips `pending_approval → queued` + `notify_run_queued`.
- `rehketo/runs/cancellation.py` — `request_cancel` re-queues parked runs so the claim path finalizes them.
- `justfile` — `worker` and `dev` recipes.
- `AGENTS.md` — bless `just dev`; document the worker; regenerate mirrors.

Deleted files:
- `rehketo/agent/sweep.py` and `tests/integration/test_startup_sweep.py` (replaced by the reaper).
- `tests/unit/test_approval_wait.py` (the waiter it tests is removed).

---

## Task 1: `heartbeat_at` column + claim/reaper indexes

**Files:**
- Create: `alembic/versions/0013_runs_heartbeat_at.py`
- Modify: `rehketo/db/models.py:198-201` (add column after `model` / before `created_at`)
- Test: `tests/integration/test_db_fixture.py` already asserts migrations apply; add a focused column test at `tests/integration/test_runs_heartbeat_column.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_runs_heartbeat_column.py`:

```python
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.db.models import Conversation, Run, User


async def test_run_persists_heartbeat_at(settings_env: object, db_url: str) -> None:
    reset_engine_for_tests()
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@x.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        db.add(user)
        await db.flush()
        db.add(conv)
        await db.flush()
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="queued",
            model="m",
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    async with sessionmaker()() as db:
        row = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        assert row.heartbeat_at is None  # column exists, defaults null
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_runs_heartbeat_column.py -v`
Expected: FAIL — `AttributeError: 'Run' object has no attribute 'heartbeat_at'` (or a missing-column DB error).

- [ ] **Step 3: Add the model column**

In `rehketo/db/models.py`, inside `class Run`, add after the `model` column (line ~198):

```python
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0013_runs_heartbeat_at.py`:

```python
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_runs_heartbeat_column.py tests/integration/test_db_fixture.py -v`
Expected: PASS (the `db_url` fixture runs `downgrade base` + `upgrade head`, exercising the new migration both ways).

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0013_runs_heartbeat_at.py rehketo/db/models.py tests/integration/test_runs_heartbeat_column.py
git commit -m "feat: add runs.heartbeat_at column and claim/reaper indexes"
```

---

## Task 2: Claim module

**Files:**
- Create: `rehketo/runs/claim.py`
- Test: `tests/integration/test_claim.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_claim.py`:

```python
from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.claim import claim_next_run
from tests.integration._helpers import mk_running_run


async def _mk_queued_run() -> str:
    """A queued run reuses mk_running_run's seed, then resets status."""
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET status='queued', started_at=NULL WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()
    return run_id


async def test_claim_marks_running_and_stamps_heartbeat(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await _mk_queued_run()

    async with sessionmaker()() as db:
        claimed = await claim_next_run(db)

    assert claimed is not None
    assert str(claimed.id) == run_id

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, heartbeat_at, started_at FROM runs WHERE id=:r"),
                {"r": run_id},
            )
        ).one()
    assert row.status == "running"
    assert row.heartbeat_at is not None
    assert row.started_at is not None


async def test_claim_returns_none_when_no_queued_runs(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    async with sessionmaker()() as db:
        assert await claim_next_run(db) is None


async def test_two_claimers_get_disjoint_runs(settings_env: object, db_url: str) -> None:
    """SKIP LOCKED: concurrent claimers never grab the same run."""
    reset_engine_for_tests()
    a = await _mk_queued_run()
    b = await _mk_queued_run()

    async def claim_one() -> str | None:
        async with sessionmaker()() as db:
            c = await claim_next_run(db)
        return None if c is None else str(c.id)

    r1, r2 = await asyncio.gather(claim_one(), claim_one())
    assert {r1, r2} == {a, b}  # disjoint, both claimed, none doubled
    assert r1 != r2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_claim.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.runs.claim'`.

- [ ] **Step 3: Write the claim module**

Create `rehketo/runs/claim.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003  # used at runtime in the dataclass

from sqlalchemy import text

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

RUN_QUEUED_CHANNEL = "run_queued"


@dataclass(frozen=True)
class ClaimedRun:
    id: UUID
    conversation_id: UUID
    user_id: UUID
    cancel_requested_at: datetime | None


async def claim_next_run(db: AsyncSession) -> ClaimedRun | None:
    """Atomically claim one queued run. The UPDATE flips status to 'running'
    and stamps heartbeat_at in the same statement, so a just-claimed run can
    never look stale to a reaper. FOR UPDATE SKIP LOCKED lets N workers claim
    disjoint runs without blocking each other. Silent: no event is published —
    run_agent publishes run.status=running, keeping that the single source of
    the event. started_at is preserved across a resume via COALESCE."""
    row = (
        await db.execute(
            text(
                "UPDATE runs SET status='running', heartbeat_at=now(), "
                "started_at=COALESCE(started_at, now()) "
                "WHERE id = (SELECT id FROM runs WHERE status='queued' "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING id, conversation_id, user_id, cancel_requested_at"
            )
        )
    ).one_or_none()
    await db.commit()
    if row is None:
        return None
    return ClaimedRun(
        id=row.id,
        conversation_id=row.conversation_id,
        user_id=row.user_id,
        cancel_requested_at=row.cancel_requested_at,
    )


async def notify_run_queued(db: AsyncSession, run_id: UUID) -> None:
    """Ring the doorbell so an idle worker claims promptly. Call within the
    same transaction that inserts/flips the run to 'queued' — postgres delivers
    NOTIFY on commit, so the wake can never precede the row. A missed NOTIFY
    costs only latency: workers also poll."""
    await db.execute(
        text("SELECT pg_notify(:chan, :rid)"),
        {"chan": RUN_QUEUED_CHANNEL, "rid": str(run_id)},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_claim.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add rehketo/runs/claim.py tests/integration/test_claim.py
git commit -m "feat: add SKIP LOCKED run claim + run_queued doorbell helper"
```

---

## Task 3: Reaper module (additive; sweep stays for now)

**Files:**
- Create: `rehketo/runs/reaper.py`
- Test: `tests/integration/test_reaper.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_reaper.py`:

```python
from __future__ import annotations

import asyncio
import contextlib
from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.reaper import reap_stale_runs
from tests.integration._helpers import mk_running_run


async def _set_heartbeat_age(run_id: str, seconds_ago: float) -> None:
    async with sessionmaker()() as db:
        await db.execute(
            text(
                "UPDATE runs SET heartbeat_at = now() - make_interval(secs => :s) "
                "WHERE id = :r"
            ),
            {"s": seconds_ago, "r": run_id},
        )
        await db.commit()


async def test_reaps_running_run_with_stale_heartbeat(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 120)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        reaped = await reap_stale_runs(bus, threshold_seconds=60)
    finally:
        await bus.stop()

    assert UUID(run_id) in reaped
    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, error FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).one()
    assert row.status == "failed"
    assert row.error["code"] == "process_restart"


async def test_does_not_reap_fresh_heartbeat(settings_env: object, db_url: str) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 1)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        reaped = await reap_stale_runs(bus, threshold_seconds=60)
    finally:
        await bus.stop()

    assert UUID(run_id) not in reaped
    async with sessionmaker()() as db:
        row = (
            await db.execute(text("SELECT status FROM runs WHERE id=:r"), {"r": run_id})
        ).one()
    assert row.status == "running"


async def test_reaper_publishes_closure_events(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()
    await _set_heartbeat_age(run_id, 120)

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    try:
        await reap_stale_runs(bus, threshold_seconds=60)

        events: list[dict] = []

        async def consume() -> None:
            async with contextlib.aclosing(bus.subscribe(run_id)) as stream:
                async for e in stream:
                    events.append(e)
                    if e["type"] == "run.ended":
                        return

        await asyncio.wait_for(consume(), timeout=10)
    finally:
        await bus.stop()

    statuses = [e for e in events if e["type"] == "run.status"]
    assert statuses[-1]["status"] == "failed"
    assert events[-1]["type"] == "run.ended"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_reaper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.runs.reaper'`.

- [ ] **Step 3: Write the reaper module**

Create `rehketo/runs/reaper.py`:

```python
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: TC003  # returned at runtime

from sqlalchemy import text

from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker

if TYPE_CHECKING:
    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

REAP_INTERVAL_SECONDS = 30.0
REAP_THRESHOLD_SECONDS = 60.0


async def reap_stale_runs(
    bus: RunEventBus, *, threshold_seconds: float = REAP_THRESHOLD_SECONDS
) -> list[UUID]:
    """Fail every 'running' run whose heartbeat is older than the threshold —
    its owning worker died. The UPDATE is idempotent, so concurrent reapers in
    sibling workers are safe with no leader election. Publishes the terminal
    pair so a subscriber attached to a dead run's stream gets a clean close
    instead of a hang. error.code matches the old sweep so the UI/tests need no
    new vocabulary."""
    error = {"code": "process_restart", "message": "run abandoned by worker crash"}
    async with sessionmaker()() as db:
        result = await db.execute(
            text(
                "UPDATE runs SET status='failed', error=CAST(:err AS jsonb), "
                "finished_at=now() "
                "WHERE status='running' "
                "AND heartbeat_at < now() - make_interval(secs => :thr) "
                "RETURNING id"
            ),
            {"err": json.dumps(error), "thr": threshold_seconds},
        )
        ids = [row.id for row in result.all()]
        await db.commit()
    for run_id in ids:
        with contextlib.suppress(Exception):
            await bus.publish(
                str(run_id),
                {"type": "run.status", "status": "failed", "error": error},
            )
            await bus.publish(str(run_id), {"type": "run.ended"})
    if ids:
        logger.info("reaped %d stale runs", len(ids))
    return ids


async def run_reaper(
    bus: RunEventBus,
    *,
    interval_seconds: float = REAP_INTERVAL_SECONDS,
    threshold_seconds: float = REAP_THRESHOLD_SECONDS,
) -> None:
    """Reap on a fixed cadence forever. Long-lived asyncio.Task owned by the
    worker. Swallows per-pass errors so a transient DB blip costs one cycle,
    not the loop."""
    while True:
        try:
            await reap_stale_runs(bus, threshold_seconds=threshold_seconds)
        except Exception:
            logger.exception("reaper pass failed")
        await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_reaper.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add rehketo/runs/reaper.py tests/integration/test_reaper.py
git commit -m "feat: add heartbeat reaper for orphaned running runs"
```

---

## Task 4: Heartbeat

**Files:**
- Create: `rehketo/runs/heartbeat.py`
- Test: `tests/integration/test_heartbeat.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_heartbeat.py`:

```python
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.heartbeat import beat
from rehketo.runs.cancellation import request_cancel
from tests.integration._helpers import mk_running_run


async def test_beat_advances_heartbeat_and_reports_no_cancel(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await mk_running_run()

    cancel_requested = await beat(UUID(run_id))
    assert cancel_requested is False

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT heartbeat_at FROM runs WHERE id=:r"), {"r": run_id}
            )
        ).one()
    assert row.heartbeat_at is not None


async def test_beat_reports_pending_cancel(settings_env: object, db_url: str) -> None:
    """The heartbeat re-reads cancel_requested_at as a backstop against a lost
    control NOTIFY."""
    reset_engine_for_tests()
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await request_cancel(db, UUID(run_id))

    assert await beat(UUID(run_id)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_heartbeat.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.runs.heartbeat'`.

- [ ] **Step 3: Write the heartbeat module**

Create `rehketo/runs/heartbeat.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_heartbeat.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add rehketo/runs/heartbeat.py tests/integration/test_heartbeat.py
git commit -m "feat: add per-run heartbeat with cancel backstop"
```

---

## Task 5: Worker loop

The worker claims runs up to a concurrency cap, supervises each (heartbeat + run_agent), reacts to a `run_queued` doorbell with a poll floor, and runs the reaper. A run already carrying `cancel_requested_at` at claim time is finalized `cancelled` without executing the graph.

**Files:**
- Create: `rehketo/runs/worker.py`
- Test: `tests/integration/test_worker_loop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_worker_loop.py`:

```python
from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.claim import notify_run_queued
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.worker import run_worker
from tests.integration._helpers import make_fake_build_agent, FakeStreamingAgent, mk_running_run


async def _queue_run() -> str:
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET status='queued', started_at=NULL WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()
    return run_id


async def _wait_status(run_id: str, status: str, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            async with sessionmaker()() as db:
                row = (
                    await db.execute(
                        text("SELECT status FROM runs WHERE id=:r"), {"r": run_id}
                    )
                ).one()
            if row.status == status:
                return
            await asyncio.sleep(0.05)


@pytest.fixture
def fake_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_mod, "build_agent", make_fake_build_agent(FakeStreamingAgent(("hi",)))
    )


async def test_worker_claims_and_runs_a_queued_run(
    settings_env: object, db_url: str, fake_agent: None
) -> None:
    reset_engine_for_tests()
    run_id = await _queue_run()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    worker = asyncio.create_task(run_worker(bus, poll_interval=0.5))
    try:
        async with sessionmaker()() as db:
            await notify_run_queued(db, UUID(run_id))
            await db.commit()
        await _wait_status(run_id, "succeeded")
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await bus.stop()


async def test_worker_finalizes_precancelled_run_without_executing(
    settings_env: object, db_url: str
) -> None:
    """A run cancelled while parked: cancel_requested_at is set and status is
    queued. The worker finalizes 'cancelled' at the claim head — build_agent is
    never called (no monkeypatch installed; a call would explode)."""
    reset_engine_for_tests()
    run_id = await _queue_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET cancel_requested_at=now() WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()

    bus = PostgresEventBus(poll_interval=0.2)
    await bus.start()
    worker = asyncio.create_task(run_worker(bus, poll_interval=0.5))
    try:
        await _wait_status(run_id, "cancelled")
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await bus.stop()

    types = []
    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id=:r ORDER BY sequence"
                ),
                {"r": run_id},
            )
        ).all()
    types = [r.payload["type"] for r in rows]
    assert types[-1] == "run.ended"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_worker_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.runs.worker'`.

- [ ] **Step 3: Write the worker module**

Create `rehketo/runs/worker.py`:

```python
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import update

from rehketo.agent.run import run_agent
from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker
from rehketo.db.models import Run
from rehketo.runs.cancellation import RunControlListener
from rehketo.runs.claim import ClaimedRun, RUN_QUEUED_CHANNEL, claim_next_run
from rehketo.runs.heartbeat import HEARTBEAT_INTERVAL_SECONDS, beat
from rehketo.runs.listen import listen
from rehketo.runs.reaper import run_reaper
from rehketo.runs.registry import RunTaskRegistry

if TYPE_CHECKING:
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

DEFAULT_CONCURRENCY = 4
DEFAULT_POLL_INTERVAL = 2.0


async def run_worker(
    bus: RunEventBus,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Claim and execute runs forever. Owns a control listener (cross-process
    cancel), a run_queued doorbell listener, and the reaper. Runs until
    cancelled; intended as the worker process's top-level coroutine."""
    registry = RunTaskRegistry()
    control = RunControlListener(registry)
    await control.start()

    wake = asyncio.Event()
    doorbell = asyncio.create_task(
        listen(RUN_QUEUED_CHANNEL, lambda _payload: wake.set())
    )
    reaper = asyncio.create_task(run_reaper(bus))
    active: set[asyncio.Task[None]] = set()

    try:
        while True:
            # Fill open slots, claiming until the queue is dry or we're full.
            while len(active) < concurrency:
                async with sessionmaker()() as db:
                    claimed = await claim_next_run(db)
                if claimed is None:
                    break
                task = asyncio.create_task(_supervise(claimed, bus, registry))
                active.add(task)
                task.add_done_callback(active.discard)
            # Sleep until a doorbell or the poll floor, whichever first.
            wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=poll_interval)
    finally:
        doorbell.cancel()
        reaper.cancel()
        for task in (doorbell, reaper):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in list(active):
            task.cancel()
        for task in list(active):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await control.stop()


async def _supervise(
    claimed: ClaimedRun, bus: RunEventBus, registry: RunTaskRegistry
) -> None:
    """Drive one claimed run: short-circuit a pre-cancelled run, else run the
    agent under a heartbeat that doubles as the lost-NOTIFY cancel backstop."""
    if claimed.cancel_requested_at is not None:
        await _finalize_precancelled(claimed.id, bus)
        return

    run_task = asyncio.create_task(run_agent(claimed.id, bus))
    registry.register(claimed.id, run_task)
    heart = asyncio.create_task(_heartbeat(claimed.id, run_task))
    try:
        await run_task
    except asyncio.CancelledError:
        # Worker shutdown: cancel the run too so it doesn't leak unsupervised.
        # run_agent's shielded CancelledError finalizer marks it cancelled.
        run_task.cancel()
        with contextlib.suppress(BaseException):
            await run_task
        raise
    except Exception:
        logger.exception("run %s failed in supervisor", claimed.id)
    finally:
        heart.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heart


async def _heartbeat(run_id: UUID, run_task: asyncio.Task[None]) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        cancel_requested = await beat(run_id)
        if cancel_requested:
            run_task.cancel()
            return


async def _finalize_precancelled(run_id: UUID, bus: RunEventBus) -> None:
    """A run cancelled while parked (queued/pending_approval) carries
    cancel_requested_at at claim time. Finalize 'cancelled' without invoking
    the graph, keeping all finalization on the worker side."""
    async with sessionmaker()() as db:
        await db.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="cancelled", finished_at=datetime.now(UTC))
        )
        await db.commit()
    with contextlib.suppress(Exception):
        await bus.publish(str(run_id), {"type": "run.status", "status": "cancelled"})
        await bus.publish(str(run_id), {"type": "run.ended"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_worker_loop.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add rehketo/runs/worker.py tests/integration/test_worker_loop.py
git commit -m "feat: add agent worker claim loop with heartbeat and reaper"
```

---

## Task 6: Worker process entrypoint

**Files:**
- Create: `rehketo/cli/worker.py`
- Test: `tests/unit/test_worker_entrypoint.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_worker_entrypoint.py`:

```python
from __future__ import annotations

import asyncio
from typing import Any

import pytest

import rehketo.cli.worker as worker_cli


async def test_main_starts_and_stops_cleanly(
    settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run wires bus + worker; cancelling it tears everything down without
    raising. We stub the bus and worker so the test needs no DB."""
    started: dict[str, bool] = {"bus": False, "worker": False}

    class FakeBus:
        async def start(self) -> None:
            started["bus"] = True

        async def stop(self) -> None:
            started["bus"] = False

    async def fake_run_worker(bus: Any, **_kw: Any) -> None:
        started["worker"] = True
        await asyncio.Event().wait()  # run forever until cancelled

    monkeypatch.setattr(worker_cli, "PostgresEventBus", lambda: FakeBus())
    monkeypatch.setattr(worker_cli, "run_worker", fake_run_worker)

    task = asyncio.create_task(worker_cli._run())
    await asyncio.sleep(0.05)
    assert started["bus"] and started["worker"]
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert started["bus"] is False  # bus.stop() ran in the finally
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_worker_entrypoint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.cli.worker'`.

- [ ] **Step 3: Write the entrypoint**

Create `rehketo/cli/worker.py`:

```python
"""Agent worker entry point.

    uv run python -m rehketo.cli.worker

Claims queued runs from postgres and drives the LangGraph agent loop, isolated
from the auth-holding API process. Mirrors rehketo.cli.serve's Windows event
loop policy handling (psycopg3 async needs SelectorEventLoop).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from rehketo.core.logging import get_logger
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.worker import run_worker

logger = get_logger(__name__)


async def _run() -> None:
    bus = PostgresEventBus()
    await bus.start()
    try:
        await run_worker(bus)
    finally:
        await bus.stop()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
        )
    logger.info("rehketo agent worker starting")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_worker_entrypoint.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo/cli/worker.py tests/unit/test_worker_entrypoint.py
git commit -m "feat: add worker process entrypoint"
```

---

## Task 7: Cutover — API stops executing runs

The API stops spawning tasks and instead inserts `queued` + rings the doorbell. The startup sweep, the API-side task registry, and the API-side control listener are removed (the worker owns them). The `live_app` test helper starts a worker so POST→execute still works in-process.

**Files:**
- Modify: `rehketo/api/messages.py:15,84-89`
- Modify: `rehketo/main.py:29,84-97,116-118`
- Delete: `rehketo/agent/sweep.py`, `tests/integration/test_startup_sweep.py`
- Modify: `tests/integration/_helpers.py:35-54` (`live_app`)
- Modify: `tests/integration/test_post_messages_kicks_run.py`

- [ ] **Step 1: Update the kickoff test to expect a queued run**

Replace the body of `test_posting_a_message_creates_row_and_kicks_off_run` in `tests/integration/test_post_messages_kicks_run.py` — drop the `asyncio.sleep` and assert the run is left `queued` (the API no longer executes it):

```python
    assert r.status_code == 202
    body = r.json()
    assert UUID(body["message_id"])
    assert UUID(body["run_id"])

    # User message persisted
    msgs = (
        (await db.execute(select(Message).where(Message.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert any(m.role == "user" and m.content.get("text") == "hello" for m in msgs)

    # Run row exists and is left queued for a worker to claim — the API no
    # longer executes runs in-process.
    runs = (
        (await db.execute(select(Run).where(Run.conversation_id == conv.id)))
        .scalars()
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == "queued"
```

Remove the now-unused `import asyncio` and the `respx` mock block at the top of that test if `respx` is no longer referenced (the agent never runs here). Keep `@respx.mock`-free: delete the decorator and the `respx.post(...)` line.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_post_messages_kicks_run.py -v`
Expected: FAIL — the run transitions to running/succeeded today (still spawned), so `status == "queued"` fails.

- [ ] **Step 3: Update the kickoff handler**

In `rehketo/api/messages.py`, remove the `run_agent` import (line 15) and replace the spawn block (lines 82-89). The new tail of `post_message`:

```python
    conv.updated_at = datetime.now(UTC)
    await notify_run_queued(db, run_id)
    await db.commit()

    return MessageKickoffOut(message_id=message_id, run_id=run_id)
```

Add the import near the others:

```python
from rehketo.runs.claim import notify_run_queued
```

Delete the now-unused `import asyncio` at the top of `messages.py` and the `bus = request.app.state.event_bus` / `registry = ...` lines. (`request` may become unused; if so, drop the `request: Request` parameter and the `Request` import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_post_messages_kicks_run.py -v`
Expected: PASS.

- [ ] **Step 5: Remove the sweep and the API-side run ownership**

Delete the files:

```bash
git rm rehketo/agent/sweep.py tests/integration/test_startup_sweep.py
```

In `rehketo/main.py`:
- Remove `from rehketo.agent.sweep import sweep_abandoned_runs` (line 29).
- Remove `from rehketo.runs.cancellation import RunControlListener` and `from rehketo.runs.registry import get_registry` imports (the API no longer holds tasks).
- In `_lifespan`, drop the control listener and sweep; keep the bus:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    logger.info("rehketo-api starting app_env=%s", settings.app_env)
    try:
        await app.state.event_bus.start()
        yield
    finally:
        await app.state.event_bus.stop()
```

- In `create_app`, remove the `app.state.task_registry` and `app.state.control_listener` assignments (lines 117-118); keep `app.state.event_bus = PostgresEventBus()`.

- [ ] **Step 6: Update `live_app` to run a worker**

In `tests/integration/_helpers.py`, replace `live_app` so it starts the bus and a worker (the worker owns the control listener now):

```python
@asynccontextmanager
async def live_app() -> AsyncIterator[FastAPI]:
    """create_app() with its event bus started, plus an in-process worker that
    claims and runs queued runs — the post-M4 execution path. httpx's
    ASGITransport never runs lifespan, so we start the bus here; the worker is
    what turns a queued run into a live stream. Use in any test that posts a
    message and consumes its SSE, or cancels a run."""
    from rehketo.runs.worker import run_worker

    app = create_app()
    await app.state.event_bus.start()
    worker = asyncio.create_task(run_worker(app.state.event_bus, poll_interval=0.25))
    try:
        yield app
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await app.state.event_bus.stop()
```

Add `import contextlib` to `_helpers.py` if absent.

- [ ] **Step 7: Run the affected suites**

Run: `uv run pytest tests/integration/test_run_agent_end_to_end.py tests/integration/test_run_agent_approval.py tests/integration/test_run_cancel.py tests/integration/test_post_messages_kicks_run.py -v`
Expected: PASS. (Approval still works via the old blocking model — the worker `await`s a `run_agent` that blocks in `wait_for_decisions`; Task 8 changes that.)

- [ ] **Step 8: Run the full API suite to catch fallout**

Run: `uv run pytest`
Expected: PASS. If a test imported `sweep_abandoned_runs` or `app.state.control_listener`/`task_registry`, update or remove it (charter rule 8). Note `tests/integration/test_cancellation_control.py` constructs its own `RunControlListener`/`RunTaskRegistry` directly — it stays valid.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: API enqueues runs; worker owns execution, cancel, and reaping"
```

---

## Task 8: Release-on-interrupt approval (durable resume)

Replace the blocking approval model. When the agent interrupts for approval, the worker publishes `approval_required` (with the interrupt id for correlation), sets `pending_approval`, and **returns without finalizing** — freeing its slot. On re-claim (after the decision flips the run to `queued`), `run_agent` rebuilds the resume `Command` from the durable `run_events` and continues.

**Files:**
- Modify: `rehketo/agent/approval.py` (replace `resolve_interrupt`/`wait_for_decisions`)
- Modify: `rehketo/agent/run.py` (resume loop, parked outcome, terminal-only `run.ended`)
- Delete: `tests/unit/test_approval_wait.py`
- Modify: `tests/integration/test_run_agent_approval.py` (two-invocation resume)

- [ ] **Step 1: Rewrite the approval integration test for release/re-claim**

Replace `tests/integration/test_run_agent_approval.py`'s `test_approve_resumes_and_succeeds` and `test_deny_maps_to_reject` to model the two-phase flow (the `_InterruptingAgent`, `_seed`, `_install`, `_event_payloads`, `_wait_for_status` helpers stay; `_decide` stays). The agent must survive across two `run_agent` calls, so build it once and reuse:

```python
async def test_approve_releases_then_resumes_on_reclaim(
    settings_env, db_url, db, monkeypatch
) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    # Phase 1: run parks at pending_approval and returns (slot freed).
    await run_mod.run_agent(run_id, bus)
    await _wait_for_status(run_id, "pending_approval")
    assert agent.resume_inputs == []  # released, did not resume in-process

    # Decision arrives; the endpoint (Task 10) flips to queued. Simulate both.
    await _decide(bus, run_id, "approve")
    async with sessionmaker()() as s:
        await s.execute(
            text("UPDATE runs SET status='queued' WHERE id=:r"), {"r": str(run_id)}
        )
        await s.commit()

    # Phase 2: re-claim resumes from the checkpoint using the durable decision.
    await run_mod.run_agent(run_id, bus)

    assert len(agent.resume_inputs) == 1
    assert agent.resume_inputs[0].resume == {"intr-1": {"decisions": [{"type": "approve"}]}}

    payloads = await _event_payloads(run_id)
    statuses = [p.get("status") for p in payloads if p["type"] == "run.status"]
    assert statuses == ["running", "pending_approval", "running", "succeeded"]
    types = [p["type"] for p in payloads]
    assert types[-1] == "run.ended"
    async with sessionmaker()() as s:
        msg = (
            await s.execute(
                text("SELECT content FROM messages WHERE run_id=:r"),
                {"r": str(run_id)},
            )
        ).one()
    # Pre-approval narration ("calling…") survives the release via rehydration
    # (Task 9 makes this assertion hold; here it is at least the resume text).
    assert "done" in msg.content["text"]
```

Update `test_deny_maps_to_reject` the same two-phase way, asserting `{"type": "reject"}`. Leave `test_cancel_while_pending_finalizes_cancelled` as-is for now — it is superseded by Task 10's parked-cancel test; delete it in Task 10.

- [ ] **Step 2: Delete the waiter unit test**

```bash
git rm tests/unit/test_approval_wait.py
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_run_agent_approval.py::test_approve_releases_then_resumes_on_reclaim -v`
Expected: FAIL — today `run_agent` blocks at the interrupt instead of returning; `resume_inputs` is non-empty after phase 1 / the call never returns.

- [ ] **Step 4: Rewrite `approval.py`**

Replace the whole body of `rehketo/agent/approval.py` (module docstring updated, `wait_for_decisions` and `resolve_interrupt` removed):

```python
"""Pause/resume plumbing for per-call tool approval.

The HITL middleware interrupts the graph BEFORE an untrusted tool executes.
On the first encounter the worker publishes one durable tool.approval_required
per call (carrying the interrupt id for correlation), parks the run at
pending_approval, and releases its slot. When the decision arrives the run is
re-queued; on re-claim build_resume_command reconstructs the resume Command
from the journaled approval_required + approval_decision events. Decisions are
the durable source of truth, so resume is correct across processes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import text

from rehketo.db import sessionmaker

if TYPE_CHECKING:
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus


def _interrupt(state: Any) -> Any | None:
    interrupts = [i for task in state.tasks for i in task.interrupts]
    return interrupts[0] if interrupts else None


async def park_on_interrupt(
    agent: Any, config: dict[str, Any], *, run_id: UUID, bus: RunEventBus
) -> bool:
    """If the graph paused on approval, publish one approval_required per call,
    set pending_approval, and return True (the caller releases the run). Return
    False if there is no interrupt (the turn finished). Idempotent against a
    re-encounter: if approval_required already exists for this interrupt id we
    do not re-publish."""
    state = await agent.aget_state(config)
    intr = _interrupt(state)
    if intr is None:
        return False
    if not await _required_ids(run_id, intr.id):
        requests = intr.value["action_requests"]
        for request in requests:
            await bus.publish(
                str(run_id),
                {
                    "type": "tool.approval_required",
                    "approval_id": str(uuid4()),
                    "interrupt_id": intr.id,
                    "tool": request["name"],
                    "arguments": request["args"],
                },
            )
    await _set_status(run_id, "pending_approval", bus)
    return True


async def build_resume_command(
    agent: Any, config: dict[str, Any], *, run_id: UUID
) -> Command[Any] | None:
    """On re-claim, reconstruct the resume Command from durable events. Returns
    None if the checkpoint no longer holds an interrupt (already resumed). Pure
    read: it does NOT publish run.status=running — run_agent's start block
    already did when the re-claimed run flipped to running, so the resume emits
    exactly one running event."""
    state = await agent.aget_state(config)
    intr = _interrupt(state)
    if intr is None:
        return None
    ids = await _required_ids(run_id, intr.id)  # publish order == request order
    decisions = await _decisions_for(run_id, ids)
    # Wire vocabulary approve/deny -> middleware approve/reject. A bare reject
    # tells the model the tool was not executed and not to retry (deny).
    return Command(
        resume={
            intr.id: {
                "decisions": [
                    {"type": "approve"}
                    if decisions.get(approval_id) == "approve"
                    else {"type": "reject"}
                    for approval_id in ids
                ]
            }
        }
    )


async def _required_ids(run_id: UUID, interrupt_id: str) -> list[str]:
    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload->>'approval_id' AS aid FROM run_events "
                    "WHERE run_id=:r AND payload->>'type'='tool.approval_required' "
                    "AND payload->>'interrupt_id'=:i ORDER BY sequence"
                ),
                {"r": str(run_id), "i": interrupt_id},
            )
        ).all()
    return [row.aid for row in rows]


async def _decisions_for(run_id: UUID, approval_ids: list[str]) -> dict[str, str]:
    if not approval_ids:
        return {}
    async with sessionmaker()() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT payload->>'approval_id' AS aid, "
                    "payload->>'decision' AS dec FROM run_events "
                    "WHERE run_id=:r AND payload->>'type'='tool.approval_decision' "
                    "AND payload->>'approval_id' = ANY(:ids) ORDER BY sequence"
                ),
                {"r": str(run_id), "ids": approval_ids},
            )
        ).all()
    # First decision per id wins.
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(row.aid, row.dec)
    return out


async def _set_status(run_id: UUID, status: str, bus: RunEventBus) -> None:
    async with sessionmaker()() as db:
        # Safe under multi-worker: a parked run has exactly one claimer at a
        # time — the SKIP LOCKED claim is the mutex.
        await db.execute(
            text("UPDATE runs SET status=:s WHERE id=:r"),
            {"s": status, "r": str(run_id)},
        )
        await db.commit()
    await bus.publish(str(run_id), {"type": "run.status", "status": status})
```

- [ ] **Step 5: Rewrite `run_agent`'s resume loop**

In `rehketo/agent/run.py`:

(a) Replace the import `from rehketo.agent.approval import resolve_interrupt` with `from rehketo.agent.approval import build_resume_command, park_on_interrupt`.

(b) Add a function-scoped `parked` flag immediately after `segments = SegmentTracker()` (line 161), so it is bound before the outer `try`/`finally` regardless of which path runs:

```python
    segments = SegmentTracker()
    parked = False
```

(c) Replace the streaming block (lines 202-232) so a resuming run rebuilds the command and a fresh interrupt parks-and-returns:

```python
            async with contextlib.AsyncExitStack() as stack:
                tools, interrupt_on = await build_run_toolset(
                    stack, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id), system_prompt, tools=tools, interrupt_on=interrupt_on
                ):
                    config: Any = {"configurable": {"thread_id": str(run_id)}}
                    # A run with a pending interrupt in its checkpoint is a
                    # resume: rebuild the decision-bearing Command from the
                    # durable journal instead of restarting from history.
                    resume_cmd = (
                        await build_resume_command(agent, config, run_id=run_id)
                        if interrupt_on
                        else None
                    )
                    if resume_cmd is not None:
                        # Replay prior deltas so persisted text spans the
                        # approval boundary (Task 9 fills in _rehydrate_segments).
                        async with sessionmaker()() as db:
                            await _rehydrate_segments(db, run_id, segments)
                    stream_input: Any = (
                        resume_cmd if resume_cmd is not None else {"messages": history}
                    )
                    while True:
                        async for chunk in agent.astream(
                            stream_input,
                            config=config,
                            stream_mode="messages",
                        ):
                            for event in transform_chunk(chunk):  # type: ignore[arg-type]
                                await bus.publish(str(run_id), event)
                                if event["type"] == "message.delta":
                                    segments.add_delta(
                                        event.get("message_id"), str(event["delta"])
                                    )
                        if not interrupt_on:
                            break
                        if await park_on_interrupt(
                            agent, config, run_id=run_id, bus=bus
                        ):
                            parked = True
                            break
                        break
                    if parked:
                        return  # non-terminal: stay pending_approval, no run.ended
```

Note: the `_rehydrate_segments` call is included here but its helper is added in Task 9; until then it raises `NameError` on a resume path only. To keep Task 8 green on its own, define a temporary no-op stub now and replace it in Task 9:

```python
async def _rehydrate_segments(db: AsyncSession, run_id: UUID, segments: SegmentTracker) -> None:
    return  # filled in Task 9
```

(d) The early `return` runs the outer `finally`. Guard the terminator so a parked run does NOT publish `run.ended` (its stream stays open for the resume). Change the `finally` block (lines 395-402):

```python
    finally:
        # Single, guaranteed terminator — but only for terminal outcomes. A
        # parked run returned early above and must keep its stream open.
        with contextlib.suppress(Exception):
            if not parked:
                await bus.publish(str(run_id), {"type": "run.ended"})
```

- [ ] **Step 6: Run the approval test to verify it passes (resume text only)**

Run: `uv run pytest tests/integration/test_run_agent_approval.py -v`
Expected: `test_approve_releases_then_resumes_on_reclaim` and `test_deny_maps_to_reject` PASS. (The persisted message holds only the resume text `"done"` for now; rehydration of `"calling…"` lands in Task 9.)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: release-on-interrupt approval with durable journal-based resume"
```

---

## Task 9: Segment rehydration across the approval boundary

The release drops the first worker's in-memory `SegmentTracker`. On resume, rebuild it from the durable `message.delta` events so the final persisted assistant message spans pre- and post-approval narration.

**Files:**
- Modify: `rehketo/agent/run.py` (rehydrate on resume)
- Test: `tests/integration/test_run_agent_approval.py` (tighten the message assertion)

- [ ] **Step 1: Tighten the message assertion**

In `test_approve_releases_then_resumes_on_reclaim`, change the final assertion to require the full turn:

```python
    assert msg.content["text"] == "calling…done"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_run_agent_approval.py::test_approve_releases_then_resumes_on_reclaim -v`
Expected: FAIL — `assert 'done' == 'calling…done'` (pre-approval text was lost with the released tracker).

- [ ] **Step 3: Replace the Task 8 stub with the real rehydration**

The call site (`if resume_cmd is not None: ... await _rehydrate_segments(...)`) already exists from Task 8. Replace the no-op `_rehydrate_segments` stub body in `rehketo/agent/run.py` with the real implementation that replays prior deltas into the `SegmentTracker`:

```python
async def _rehydrate_segments(
    db: AsyncSession, run_id: UUID, segments: SegmentTracker
) -> None:
    """Rebuild streaming-segment state from the durable delta journal so a run
    resumed after an approval release persists its pre-release narration too."""
    rows = (
        (
            await db.execute(
                select(RunEvent.payload)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.payload["type"].astext == "message.delta",
                )
                .order_by(RunEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    for payload in rows:
        segments.add_delta(payload.get("message_id"), str(payload.get("delta", "")))
```

`RunEvent`, `select`, `SegmentTracker`, `AsyncSession` (TYPE_CHECKING), and `UUID` are all already imported in `run.py` (used by `_delta_times`/`_assistant_rows`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_run_agent_approval.py -v`
Expected: PASS — the persisted message is `"calling…done"`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: rehydrate segment tracker on resume so narration spans approval"
```

---

## Task 10: Parked-run cancellation + decision re-queue

`decide_approval` flips `pending_approval → queued` and rings the doorbell so a worker resumes. `request_cancel` re-queues a parked run so the worker's claim head finalizes it `cancelled`. Both unify on "make the run claimable; the worker finalizes."

**Files:**
- Modify: `rehketo/api/runs.py` (`decide_approval`)
- Modify: `rehketo/runs/cancellation.py` (`request_cancel` re-queues parked runs)
- Test: `tests/integration/test_run_approvals.py`, `tests/integration/test_parked_cancel.py`

- [ ] **Step 1: Write the decision re-queue test**

Add to `tests/integration/test_run_approvals.py`, reusing its existing
`_seed_pending_run` and `_auth` helpers — after a decision POST the run flips to
`queued` so a worker re-claims and resumes:

```python
async def test_decision_requeues_run_for_resume(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 204
    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": run_id}
            )
        ).one()
    assert row.status == "queued"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_run_approvals.py::test_decision_requeues_run_for_resume -v`
Expected: FAIL — the decision endpoint publishes the event but leaves status `pending_approval`.

- [ ] **Step 3: Update `decide_approval`**

In `rehketo/api/runs.py`, after the existing `event_bus.publish(... tool.approval_decision ...)` call, flip and notify within the request session:

```python
    await request.app.state.event_bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": approval_id,
            "decision": payload.decision,
        },
    )
    # Re-queue so a worker re-claims and resumes from the checkpoint. The
    # decision is already durable in run_events; status is the claim trigger.
    await db.execute(
        text("UPDATE runs SET status='queued' WHERE id=:r AND status='pending_approval'"),
        {"r": str(run_id)},
    )
    await notify_run_queued(db, run_id)
    await db.commit()
```

Add imports to `runs.py`: `from sqlalchemy import text` (alongside `select`) and `from rehketo.runs.claim import notify_run_queued`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/integration/test_run_approvals.py -v`
Expected: PASS.

- [ ] **Step 5: Write the parked-cancel test**

Create `tests/integration/test_parked_cancel.py`:

```python
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.cancellation import request_cancel
from tests.integration._helpers import mk_pending_approval_run


async def test_request_cancel_requeues_parked_run(
    settings_env: object, db_url: str
) -> None:
    """Cancelling a parked run stamps cancel_requested_at AND flips it to queued
    so the worker's claim head finalizes it cancelled."""
    reset_engine_for_tests()
    run_id = await mk_pending_approval_run()

    async with sessionmaker()() as db:
        assert await request_cancel(db, UUID(run_id)) is True

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, cancel_requested_at FROM runs WHERE id=:r"),
                {"r": run_id},
            )
        ).one()
    assert row.status == "queued"
    assert row.cancel_requested_at is not None
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/integration/test_parked_cancel.py -v`
Expected: FAIL — `request_cancel` leaves status `pending_approval`.

- [ ] **Step 7: Update `request_cancel`**

In `rehketo/runs/cancellation.py`, after the terminal-guarded `cancel_requested_at` UPDATE succeeds, re-queue a parked run and ring the doorbell so the claim path finalizes it. Insert before the existing `pg_notify` on the control channel:

```python
    # A parked run (queued already, or pending_approval) has no task to cancel
    # via the control channel — route it through the claim instead: make it
    # claimable and let the worker finalize it cancelled at the claim head.
    await db.execute(
        text(
            "UPDATE runs SET status='queued' "
            "WHERE id=:r AND status='pending_approval'"
        ),
        {"r": str(run_id)},
    )
    await db.execute(
        text("SELECT pg_notify(:chan, :rid)"),
        {"chan": RUN_QUEUED_CHANNEL, "rid": str(run_id)},
    )
```

Add `from rehketo.runs.claim import RUN_QUEUED_CHANNEL` to `cancellation.py`. (The control-channel NOTIFY stays — a `running` run is still cancelled via its owning worker's listener; the re-queue UPDATE is a no-op for a `running` run because the `WHERE status='pending_approval'` guard does not match.)

- [ ] **Step 8: Run to verify it passes, then delete the superseded test**

Run: `uv run pytest tests/integration/test_parked_cancel.py -v`
Expected: PASS.

Delete `test_cancel_while_pending_finalizes_cancelled` from `tests/integration/test_run_agent_approval.py` (the parked-cancel path is now owned by the worker + `request_cancel`, covered by `test_parked_cancel.py` and `test_worker_loop.py::test_worker_finalizes_precancelled_run_without_executing`).

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat: route parked-run cancel and approval resume through the claim"
```

---

## Task 11: Dev recipes + AGENTS.md

**Files:**
- Modify: `justfile` (after the `api` recipe, ~line 27)
- Modify: `AGENTS.md` (the justfile bullet under "Where things live")
- Regenerate: `CLAUDE.md`, `.github/copilot-instructions.md`, `.cursor/rules/main.mdc` via the sync tool

- [ ] **Step 1: Add the worker and dev recipes**

In `justfile`, after the `api` recipe:

```just
# Run the agent worker (foreground). Claims and executes queued runs.
[working-directory("rehketo-api")]
worker:
    @test -f .env || { echo "rehketo-api/.env missing — cp .env.example .env"; exit 1; }
    uv run python -m rehketo.cli.worker

# Run api + worker together (dev convenience; prod runs them as separate services).
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT
    just api &
    just worker &
    wait
```

- [ ] **Step 2: Verify recipes are listed**

Run: `just --list`
Expected: `worker` and `dev` appear in the recipe list.

- [ ] **Step 3: Update AGENTS.md**

In `AGENTS.md`, under "Where things live", change the `justfile` bullet to bless `just dev` and add the worker:

```markdown
- `justfile` — local launch recipes (`just db`, `just api`, `just worker`,
  `just ui`, `just db-down`, and `just dev` to run api + worker together).
  Each process recipe runs in the foreground; prod runs `api` and `worker` as
  separate services. `just dev` is a one-terminal convenience that launches
  both and tears them down together.
```

Add a one-line note to the "What it is" section that execution runs in a worker process: append to the existing description that "agent runs execute in a dedicated worker process (`rehketo.cli.worker`); the API enqueues runs and streams them."

- [ ] **Step 4: Regenerate the mirrors**

Run: `uv run --project rehketo-api python tools/sync_agent_rules.py`
Then verify: `uv run --project rehketo-api python tools/sync_agent_rules.py --check`
Expected: the `--check` run reports no drift.

- [ ] **Step 5: Commit**

```bash
git add justfile AGENTS.md CLAUDE.md .github/copilot-instructions.md .cursor/rules/main.mdc
git commit -m "chore: add worker + dev just recipes; document the worker split"
```

---

## Final validation

Run the full repo validation block and quote the real output (charter rule 5).

- [ ] **Repo guards (from root):**

```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

- [ ] **API (from `rehketo-api/`):**

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run pytest -m e2e   # needs postgres up (just db)
uv run python ../tools/check_contract.py
```

The `-m e2e` suite includes `tests/e2e/test_restart_recovery.py`, which exercises the abandoned-run recovery story. Its expectations changed (the startup sweep is gone; recovery is now the reaper + worker re-claim). If it asserts sweep-specific behavior, update it to drive a worker and assert the reaper fails a heartbeat-stale run and that a `queued` run survives an API restart and is later claimed. Quote the passing output.

- [ ] **Manual smoke (optional but recommended):**

`just db`, then `just dev`. Start a run and watch it stream. Kill the worker mid-run (Ctrl-C the `just dev` terminal, or run `just api`/`just worker` in separate terminals and kill only the worker); restart the worker and confirm the reaper fails the orphaned run cleanly within ~60s. Trigger a tool approval, kill the worker while parked, restart, approve in the UI, and watch the run resume and finish with its pre-approval narration intact.
