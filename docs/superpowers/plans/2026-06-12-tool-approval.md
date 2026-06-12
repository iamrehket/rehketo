# Per-call Tool Approval (M3.5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A run pauses when the agent calls a tool from an MCP server with `auto_approve=false`; the user approves or denies from the chat UI; the run resumes (deny feeds a rejection back to the model).

**Architecture:** deepagents' built-in HITL middleware (`create_deep_agent(interrupt_on=...)`) pauses the graph before the tool executes. `run_agent`'s single `astream` call becomes a resume loop in the same asyncio task: detect the pending interrupt from checkpoint state, publish `tool.approval_required` events, flip the run to `pending_approval`, wait for `tool.approval_decision` events on the existing durable bus (published by a new `POST /runs/{run_id}/approvals/{approval_id}` endpoint), then `astream(Command(resume=...))`. Restart abandons pending runs (sweep), same as every in-flight run today.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, LangGraph 1.2 + deepagents 0.6.8 (`HumanInTheLoopMiddleware` via `langchain.agents.middleware`), Postgres LISTEN/NOTIFY bus, SvelteKit 5 + vitest.

**Spec:** `docs/superpowers/specs/2026-06-12-tool-approval-design.md`

**Working directory:** backend tasks run from `rehketo-api/`, UI tasks from `rehketo-ui/`. All paths below are repo-relative.

**Verified external contracts (do not re-derive):**
- `create_deep_agent(interrupt_on={tool_name: InterruptOnConfig(...)})` installs `HumanInTheLoopMiddleware`; tools absent from the dict auto-approve. Import: `from langchain.agents.middleware import InterruptOnConfig` (langchain 1.3.7 is already in uv.lock via deepagents; Task 4 declares it).
- The middleware interrupts ONCE per model turn with `HITLRequest = {"action_requests": [{"name", "args", "description"?}], "review_configs": [...]}` and resumes via `interrupt(...)` returning `{"decisions": [...]}` — one decision per action request, in order: `{"type": "approve"}` or `{"type": "reject", "message"?}`. Reject with no message makes the middleware tell the model the tool was not executed and not to retry.
- `langgraph.types.Interrupt` has fields `.value` (the HITLRequest) and `.id`. `Command(resume={interrupt_id: value})` targets a specific interrupt.
- Interrupts do NOT surface in `stream_mode="messages"` chunks; after `astream` ends, read `(await agent.aget_state(config)).tasks[*].interrupts`.

---

### Task 1: Migration 0012 + model changes

**Files:**
- Create: `rehketo-api/alembic/versions/0012_tool_approval.py`
- Modify: `rehketo-api/rehketo/db/models.py` (McpServer + Run)

- [ ] **Step 1: Write the migration**

```python
"""per-call tool approval: mcp_servers.auto_approve + pending_approval status

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-12 00:00:00.000000+00:00

auto_approve defaults false for ALL rows including existing ones (spec:
approval-required is the safe default; admins flip trusted servers).
runs.status gains 'pending_approval' — a non-terminal state between
'running' stints while the task waits for a user decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STATUSES_OLD = "('queued','running','succeeded','failed','cancelled')"
_STATUSES_NEW = "('queued','running','pending_approval','succeeded','failed','cancelled')"


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column(
            "auto_approve",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.drop_constraint("runs_status_enum", "runs", type_="check")
    op.create_check_constraint("runs_status_enum", "runs", f"status in {_STATUSES_NEW}")


def downgrade() -> None:
    op.drop_constraint("runs_status_enum", "runs", type_="check")
    op.create_check_constraint("runs_status_enum", "runs", f"status in {_STATUSES_OLD}")
    op.drop_column("mcp_servers", "auto_approve")
```

- [ ] **Step 2: Update the models**

In `rehketo/db/models.py`, add `text` to the existing `from sqlalchemy import (...)` block. In `McpServer`, after `enabled`:

```python
    auto_approve: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
```

(`default=False` so in-memory ORM construction in tests gets a real bool, `server_default` so raw inserts and the backfill work.)

In `Run.__table_args__`, replace the CheckConstraint string:

```python
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','pending_approval',"
            "'succeeded','failed','cancelled')",
            name="runs_status_enum",
        ),
    )
```

- [ ] **Step 3: Verify migration applies and tests still pass**

From `rehketo-api/`:
Run: `uv run alembic upgrade head` (against the dev DB; `just db` must be running)
Expected: `Running upgrade 0011 -> 0012`
Run: `uv run pytest tests/unit/test_models_compile.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/0012_tool_approval.py rehketo/db/models.py
git commit -m "feat(api): auto_approve flag and pending_approval run status (migration 0012)"
```

---

### Task 2: Permission action `chat.approve_tool_call`

**Files:**
- Modify: `rehketo-api/rehketo/permissions/actions.py`
- Modify: `rehketo-api/rehketo/permissions/roles.py`
- Test: `rehketo-api/tests/unit/test_actions_vocabulary.py`

- [ ] **Step 1: Extend the vocabulary test**

In `test_contains_expected_actions`, add to the `required` set:

```python
        "chat.approve_tool_call",
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_actions_vocabulary.py -q`
Expected: FAIL — `chat.approve_tool_call` not in ACTIONS

- [ ] **Step 3: Add the action**

`actions.py` — in `ACTIONS`, after `"chat.use_mcp_server",`:

```python
    "chat.approve_tool_call",
```

`roles.py` — add `"chat.approve_tool_call",` to BOTH the `Moderator` and `User` frozensets (after `"chat.use_mcp_server",` in each; `Admin` is `frozenset(ACTIONS)` and needs no edit). Spec rule: granted to every role with `chat.write` — the M3 precedent.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_actions_vocabulary.py tests/unit/test_permissions_check.py tests/integration/test_capabilities.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rehketo/permissions/actions.py rehketo/permissions/roles.py tests/unit/test_actions_vocabulary.py
git commit -m "feat(api): chat.approve_tool_call permission action"
```

---

### Task 3: Admin API exposes `auto_approve`

**Files:**
- Modify: `rehketo-api/rehketo/api/mcp_servers.py`
- Test: `rehketo-api/tests/integration/test_mcp_servers_admin.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mcp_servers_admin.py`:

```python
async def test_auto_approve_defaults_false_and_patches(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        assert r.json()["auto_approve"] is False
        server_id = r.json()["id"]

        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"auto_approve": True},
            **_auth(sid, csrf),
        )
        assert r.status_code == 200
        assert r.json()["auto_approve"] is True

        r = await c.get("/admin/mcp-servers", cookies={SESSION_COOKIE: sid})
        assert r.json()["items"][0]["auto_approve"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_servers_admin.py::test_auto_approve_defaults_false_and_patches -q`
Expected: FAIL — KeyError `'auto_approve'`

- [ ] **Step 3: Implement**

In `rehketo/api/mcp_servers.py`:

- `McpServerCreate`: add `auto_approve: bool = False` (after `enabled`).
- `McpServerPatch`: add `auto_approve: bool | None = None`.
- `McpServerOut`: add `auto_approve: bool` (after `enabled`).
- `_to_out`: add `auto_approve=s.auto_approve,`.
- `create_server`: add `auto_approve=payload.auto_approve,` to the `McpServer(...)` constructor.
- `patch_server`: after the `enabled` branch add:

```python
    if payload.auto_approve is not None:
        server.auto_approve = payload.auto_approve
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_mcp_servers_admin.py -q`
Expected: PASS (all, including the new test)

- [ ] **Step 5: Commit**

```bash
git add rehketo/api/mcp_servers.py tests/integration/test_mcp_servers_admin.py
git commit -m "feat(api): auto_approve on mcp server admin API"
```

---

### Task 4: `build_agent` gains `interrupt_on`; declare langchain dependency

**Files:**
- Modify: `rehketo-api/pyproject.toml`
- Modify: `rehketo-api/rehketo/agent/graph.py`

- [ ] **Step 1: Declare the dependency**

In `rehketo-api/pyproject.toml` dependencies, after the `"deepagents>=0.5.3",` line, add:

```toml
    "langchain>=1.3.7",
```

(We import `langchain.agents.middleware.InterruptOnConfig` directly; it was previously only a transitive dep of deepagents.) Then run `uv lock && uv sync` — the lock should change only by adding langchain as a direct requirement (it's already resolved at 1.3.7).

- [ ] **Step 2: Add the parameter (backward-compatible default)**

Replace `build_agent` in `rehketo/agent/graph.py`:

```python
async def build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[BaseTool] = (),
    interrupt_on: Mapping[str, InterruptOnConfig] | None = None,
) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. Tools and the per-tool approval config are
    assembled by the caller (rehketo.mcp.registry) so graph construction
    stays a pure function of its inputs. interrupt_on installs deepagents'
    HumanInTheLoopMiddleware: listed tools pause the graph for approval
    before executing; unlisted tools auto-approve.
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=list(tools),
            system_prompt=system_prompt,
            model=build_chat_model(),
            checkpointer=saver,
            interrupt_on=dict(interrupt_on) if interrupt_on else None,
        )
        yield agent
```

Add to the `TYPE_CHECKING` block: `from collections.abc import AsyncIterator, Mapping, Sequence` (Mapping is new) and `from langchain.agents.middleware import InterruptOnConfig`.

- [ ] **Step 3: Verify**

Run: `uv run ruff check rehketo/agent/graph.py && uv run mypy rehketo && uv run pytest tests/unit -q`
Expected: all clean/PASS

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock rehketo/agent/graph.py
git commit -m "feat(api): build_agent accepts interrupt_on for HITL middleware"
```

---

### Task 5: Registry returns `(tools, interrupt_on)`

**Files:**
- Modify: `rehketo-api/rehketo/mcp/registry.py`
- Modify: `rehketo-api/rehketo/agent/run.py` (call site only)
- Test: `rehketo-api/tests/integration/test_mcp_registry.py`, `rehketo-api/tests/integration/test_run_agent_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_mcp_registry.py` (note `_row` gains an `auto_approve` parameter in step 3):

```python
async def test_interrupt_on_built_from_auto_approve(settings_env, monkeypatch) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools, interrupt_on = await registry.build_run_toolset(
            stack,
            [_row("untrusted"), _row("trusted", auto_approve=True)],
            run_id="r1",
            bus=bus,
        )
    assert {t.name for t in tools} == {"untrusted__echo", "trusted__echo"}
    # Only tools from auto_approve=False servers require review; approve and
    # reject are the M3.5 decision vocabulary (no edit/respond).
    assert set(interrupt_on) == {"untrusted__echo"}
    assert interrupt_on["untrusted__echo"]["allowed_decisions"] == ["approve", "reject"]


async def test_all_trusted_servers_yield_empty_interrupt_on(
    settings_env, monkeypatch
) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    async with AsyncExitStack() as stack:
        _tools, interrupt_on = await registry.build_run_toolset(
            stack, [_row("trusted", auto_approve=True)], run_id="r1", bus=FakeBus()
        )
    assert interrupt_on == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_mcp_registry.py -q`
Expected: new tests FAIL (`cannot unpack` / unexpected keyword `auto_approve`); existing tests still pass.

- [ ] **Step 3: Implement**

In `tests/integration/test_mcp_registry.py`, extend `_row`:

```python
def _row(
    name: str,
    url: str = "https://unused.example.com/mcp",
    *,
    auto_approve: bool = False,
) -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=url,
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=auto_approve,
    )
```

In `rehketo/mcp/registry.py`:

- Add runtime import: `from langchain.agents.middleware import InterruptOnConfig` (it's a TypedDict constructed at runtime — NOT under TYPE_CHECKING).
- Change `build_run_toolset`'s signature and body:

```python
async def build_run_toolset(
    stack: AsyncExitStack,
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> tuple[list[StructuredTool], dict[str, InterruptOnConfig]]:
    """Returns the adapted tools plus the HITL interrupt config: every tool
    from an auto_approve=False server requires per-call user approval
    (approve/reject only — the M3.5 decision vocabulary); tools from
    trusted servers are absent, which the middleware auto-approves."""
    tools: list[StructuredTool] = []
    interrupt_on: dict[str, InterruptOnConfig] = {}
```

and inside the inner tool loop, after `tools.append(...)`:

```python
            if not server.auto_approve:
                interrupt_on[full_name] = InterruptOnConfig(
                    allowed_decisions=["approve", "reject"]
                )
```

and the final line becomes `return tools, interrupt_on`.

Update the two existing call-site unpacks:

- `tests/integration/test_mcp_registry.py` — the existing tests call `tools = await registry.build_run_toolset(...)`; change each to `tools, _interrupt_on = await registry.build_run_toolset(...)`.
- `rehketo/agent/run.py` line ~124: 

```python
                tools, interrupt_on = await build_run_toolset(
                    stack, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id), system_prompt, tools=tools, interrupt_on=interrupt_on
                ):
```

Because `run.py` now passes `interrupt_on=` as a keyword, every fake `build_agent` in tests must accept it. Update these signatures (add the parameter; the body stays the same):

- `tests/integration/_helpers.py` `make_fake_build_agent._build`:

```python
    async def _build(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[FakeStreamingAgent]:
        yield agent
```

- `tests/integration/test_run_agent_tools.py` — all three `_fake_build_agent` functions get the same added parameter `interrupt_on: Any = None`.
- Run `grep -rn "system_prompt: str, tools" tests/` — apply the identical signature change to any other fake `build_agent` that surfaces (search is the guard; the known ones are the four above).

Also in `tests/integration/test_run_agent_tools.py::_seed`, add `auto_approve=True,` to the `McpServer(...)` constructor — these tests assert M3 auto-execute behavior, which is now the trusted-server path.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_mcp_registry.py tests/integration/test_run_agent_tools.py -q && uv run mypy rehketo`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add rehketo/mcp/registry.py rehketo/agent/run.py tests/integration/test_mcp_registry.py tests/integration/test_run_agent_tools.py tests/integration/_helpers.py
git commit -m "feat(api): registry builds interrupt_on from auto_approve"
```

---

### Task 6: `rehketo/agent/approval.py` — decision wait + interrupt resolution

**Files:**
- Create: `rehketo-api/rehketo/agent/approval.py`
- Test: `rehketo-api/tests/unit/test_approval_wait.py`

- [ ] **Step 1: Write the failing unit test**

Create `tests/unit/test_approval_wait.py`:

```python
"""wait_for_decisions consumes the run's event stream and returns once every
approval id in the batch has a decision. Unknown and duplicate approval ids
are ignored (the endpoint validates; the waiter just filters)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rehketo.agent.approval import wait_for_decisions


class FakeBus:
    """Replays scripted events, then blocks forever (like a live stream)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        raise AssertionError("waiter must not publish")

    async def subscribe(self, run_id: str, *, from_sequence: int | None = None):
        for e in self._events:
            yield e
        await asyncio.Event().wait()  # block: the waiter must return on its own


async def test_returns_when_all_decided() -> None:
    bus = FakeBus(
        [
            {"type": "message.delta", "delta": "x"},
            {"type": "tool.approval_decision", "approval_id": "a1", "decision": "approve"},
            {"type": "tool.approval_decision", "approval_id": "a2", "decision": "deny"},
        ]
    )
    decisions = await wait_for_decisions(bus, "r1", ["a1", "a2"])
    assert decisions == {"a1": "approve", "a2": "deny"}


async def test_ignores_unknown_and_duplicate_ids() -> None:
    bus = FakeBus(
        [
            {"type": "tool.approval_decision", "approval_id": "ghost", "decision": "deny"},
            {"type": "tool.approval_decision", "approval_id": "a1", "decision": "approve"},
            {"type": "tool.approval_decision", "approval_id": "a1", "decision": "deny"},
        ]
    )
    decisions = await wait_for_decisions(bus, "r1", ["a1"])
    assert decisions == {"a1": "approve"}  # first decision wins


async def test_blocks_until_decided() -> None:
    bus = FakeBus([])
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await wait_for_decisions(bus, "r1", ["a1"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_approval_wait.py -q`
Expected: FAIL — `ModuleNotFoundError: rehketo.agent.approval`

- [ ] **Step 3: Implement the module**

Create `rehketo/agent/approval.py`:

```python
"""Pause/resume plumbing for per-call tool approval (M3.5).

The HITL middleware interrupts the graph BEFORE an untrusted tool executes;
run_agent calls resolve_interrupt after each astream stint. The decision
travels as a durable `tool.approval_decision` event on the existing bus
(published by POST /runs/{id}/approvals/{approval_id}), so it is journaled
for transcript reload and audit, and the transport is multi-process-correct
the same way the bus already is.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy import update

from rehketo.db import sessionmaker
from rehketo.db.models import Run

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus


async def resolve_interrupt(
    agent: Any, config: dict[str, Any], *, run_id: UUID, bus: RunEventBus
) -> Command | None:
    """Return a resume Command if the graph paused on tool approval, else None.

    Blocks (cancellably) until the user decides every call in the batch —
    the middleware interrupts once per model turn with ALL calls needing
    review, and the graph can only resume whole.
    """
    state = await agent.aget_state(config)
    interrupts = [i for task in state.tasks for i in task.interrupts]
    if not interrupts:
        return None
    intr = interrupts[0]
    requests = intr.value["action_requests"]
    approval_ids = [str(uuid4()) for _ in requests]
    for approval_id, request in zip(approval_ids, requests, strict=True):
        await bus.publish(
            str(run_id),
            {
                "type": "tool.approval_required",
                "approval_id": approval_id,
                "tool": request["name"],
                "arguments": request["args"],
            },
        )
    await _set_status(run_id, "pending_approval", bus)
    decisions = await wait_for_decisions(bus, str(run_id), approval_ids)
    await _set_status(run_id, "running", bus)
    # Wire vocabulary is approve/deny; the middleware's is approve/reject.
    # Reject without a message makes the middleware tell the model the tool
    # was not executed and not to retry — the spec's deny semantics.
    return Command(
        resume={
            intr.id: {
                "decisions": [
                    {"type": "approve"}
                    if decisions[approval_id] == "approve"
                    else {"type": "reject"}
                    for approval_id in approval_ids
                ]
            }
        }
    )


async def wait_for_decisions(
    bus: RunEventBus, run_id: str, approval_ids: Sequence[str]
) -> dict[str, str]:
    """Collect tool.approval_decision events until the batch is resolved.

    Subscribes from sequence 0: approval ids are fresh UUIDs, so replayed
    history can never false-match, and replay-from-start needs no
    "current sequence" bookkeeping. First decision per id wins.
    """
    pending = set(approval_ids)
    decisions: dict[str, str] = {}
    async with contextlib.aclosing(bus.subscribe(run_id)) as events:
        async for event in events:
            if event.get("type") != "tool.approval_decision":
                continue
            approval_id = str(event.get("approval_id", ""))
            if approval_id not in pending:
                continue
            decisions[approval_id] = str(event["decision"])
            pending.discard(approval_id)
            if not pending:
                return decisions
    msg = "event stream ended before approvals resolved"  # pragma: no cover  # subscribe() only ends by cancellation, which raises past us
    raise RuntimeError(msg)  # pragma: no cover


async def _set_status(run_id: UUID, status: str, bus: RunEventBus) -> None:
    async with sessionmaker()() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(status=status))
        await db.commit()
    await bus.publish(str(run_id), {"type": "run.status", "status": status})
```

(`_set_status` deliberately writes only `status` — no `started_at`/`finished_at`; `pending_approval` is not terminal.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_approval_wait.py -q && uv run ruff check rehketo/agent/approval.py && uv run mypy rehketo`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add rehketo/agent/approval.py tests/unit/test_approval_wait.py
git commit -m "feat(api): approval module - interrupt resolution and decision wait"
```

---

### Task 7: Resume loop in `run_agent`

**Files:**
- Modify: `rehketo-api/rehketo/agent/run.py`
- Modify: `rehketo-api/tests/integration/_helpers.py`
- Test: `rehketo-api/tests/integration/test_run_agent_approval.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_run_agent_approval.py`:

```python
"""run_agent's resume loop against a fake agent that emulates the deepagents
HITL contract: first astream stint ends with a pending interrupt; the resume
input must be a Command carrying {"decisions": [...]} keyed by interrupt id;
the second stint yields the final text. The REAL middleware contract is
pinned separately by the live_deps test in this file's sibling
(test_run_agent_approval_live.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def _seed(db) -> Any:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="queued",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    db.add(
        McpServer(
            id=uuid4(),
            name="testsrv",
            url="https://unused.example.com/mcp",
            auth_token_ct=None,
            allowed_roles=["User"],
            enabled=True,
            auto_approve=False,  # the M3.5 path under test
        )
    )
    await db.commit()
    return run.id


class _InterruptingAgent:
    """First astream stint interrupts; resume stint records the Command."""

    def __init__(self) -> None:
        self.resume_inputs: list[Any] = []
        self._streamed_once = False
        self.interrupt = Interrupt(
            value={
                "action_requests": [
                    {"name": "testsrv__echo", "args": {"text": "hi"}}
                ],
                "review_configs": [
                    {
                        "action_name": "testsrv__echo",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            },
            id="intr-1",
        )

    async def astream(
        self, stream_input: Any, *, config: Any = None, stream_mode: Any = None
    ) -> AsyncGenerator[Any]:
        if not self._streamed_once:
            self._streamed_once = True
            yield (AIMessageChunk(content="calling…", id="m1"), {})
            return  # graph paused on the interrupt
        self.resume_inputs.append(stream_input)
        yield (AIMessageChunk(content="done", id="m1"), {})

    async def aget_state(self, config: Any) -> Any:
        if self._streamed_once and not self.resume_inputs:
            return SimpleNamespace(
                tasks=(SimpleNamespace(interrupts=(self.interrupt,)),)
            )
        return SimpleNamespace(tasks=())


def _install(monkeypatch, agent: _InterruptingAgent) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[_InterruptingAgent]:
        assert interrupt_on, "auto_approve=False server must produce interrupt config"
        yield agent

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)


async def _event_payloads(run_id) -> list[dict[str, Any]]:
    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    return [r.payload for r in rows]


async def _wait_for_status(run_id, status: str, timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            async with sessionmaker()() as s:
                row = (
                    await s.execute(
                        text("SELECT status FROM runs WHERE id = :rid"),
                        {"rid": str(run_id)},
                    )
                ).one()
            if row.status == status:
                return
            await asyncio.sleep(0.05)


async def _decide(bus, run_id, decision: str) -> str:
    """Find the published approval id, publish a decision for it."""
    payloads = await _event_payloads(run_id)
    required = next(p for p in payloads if p["type"] == "tool.approval_required")
    approval_id = required["approval_id"]
    await bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": approval_id,
            "decision": decision,
        },
    )
    return approval_id


async def test_approve_resumes_and_succeeds(settings_env, db_url, db, monkeypatch) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    await _decide(bus, run_id, "approve")
    await asyncio.wait_for(task, timeout=10)

    # Resume payload matches the middleware contract, keyed by interrupt id.
    assert len(agent.resume_inputs) == 1
    cmd = agent.resume_inputs[0]
    assert cmd.resume == {"intr-1": {"decisions": [{"type": "approve"}]}}

    payloads = await _event_payloads(run_id)
    types = [p["type"] for p in payloads]
    # pending_approval status event sits between the request and the decision;
    # the run flips back to running before the resume stint streams.
    i_req = types.index("tool.approval_required")
    i_dec = types.index("tool.approval_decision")
    statuses = [p.get("status") for p in payloads if p["type"] == "run.status"]
    assert statuses == ["running", "pending_approval", "running", "succeeded"]
    assert i_req < i_dec
    assert types[-1] == "run.ended"
    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
    assert row.status == "succeeded"
    # Both stints' text assembled into the persisted assistant message.
    msg = (
        await s.execute(
            text("SELECT content FROM messages WHERE run_id = :rid"),
            {"rid": str(run_id)},
        )
    ).one()
    assert msg.content["text"] == "calling…done"


async def test_deny_maps_to_reject(settings_env, db_url, db, monkeypatch) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    await _decide(bus, run_id, "deny")
    await asyncio.wait_for(task, timeout=10)

    cmd = agent.resume_inputs[0]
    assert cmd.resume == {"intr-1": {"decisions": [{"type": "reject"}]}}


async def test_cancel_while_pending_finalizes_cancelled(
    settings_env, db_url, db, monkeypatch
) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
    assert row.status == "cancelled"
    types = [p["type"] for p in await _event_payloads(run_id)]
    assert types[-1] == "run.ended"
```

(Replace the inline `__import__("contextlib")` with a top-of-file `import contextlib` — written inline here only to keep the snippet self-contained.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_run_agent_approval.py -q`
Expected: FAIL — the first test hangs/fails because `run_agent` never enters a resume loop (run finishes after the first stint with the interrupt unresolved). `pending_approval` is never reached → `TimeoutError`.

- [ ] **Step 3: Implement the resume loop**

In `rehketo/agent/run.py`:

Add imports: `from langgraph.types import Command` (top-level, used in a type expression at runtime? No — only for the local variable annotation, so put it under `TYPE_CHECKING` and quote the annotation) and `from rehketo.agent.approval import resolve_interrupt` (runtime).

Replace the `async for agent in build_agent(...)` block (currently lines ~127-136, already partially updated in Task 5) with:

```python
                tools, interrupt_on = await build_run_toolset(
                    stack, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id), system_prompt, tools=tools, interrupt_on=interrupt_on
                ):
                    config = {"configurable": {"thread_id": str(run_id)}}
                    stream_input: dict[str, object] | Command = {"messages": history}
                    while True:
                        async for chunk in agent.astream(
                            stream_input,
                            config=config,
                            stream_mode="messages",
                        ):
                            for event in transform_chunk(chunk):  # type: ignore[arg-type]
                                await bus.publish(str(run_id), event)
                                if event["type"] == "message.delta":
                                    assembled_text += str(event["delta"])
                        if not interrupt_on:
                            # No HITL middleware installed — the graph cannot
                            # interrupt, so skip the checkpoint read.
                            break
                        resume = await resolve_interrupt(
                            agent, config, run_id=run_id, bus=bus
                        )
                        if resume is None:
                            break
                        stream_input = resume
```

With `Command` under `TYPE_CHECKING`, annotate as `stream_input: "dict[str, object] | Command"` — but `from __future__ import annotations` is already active in run.py, so the plain annotation works unquoted.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_run_agent_approval.py tests/integration/test_run_agent_tools.py tests/integration/test_run_agent_end_to_end.py tests/integration/test_run_ended_terminal_guarantee.py -q`
Expected: PASS — new approval tests AND the untouched M3 flows (their `interrupt_on` is empty or their servers are `auto_approve=True`, so the loop breaks after one stint exactly like today).

- [ ] **Step 5: Run the full API test suite**

Run: `uv run pytest -q`
Expected: PASS. If any other test trips on the `build_agent` keyword or a fake agent, apply the Task 5 signature fix pattern there.

- [ ] **Step 6: Commit**

```bash
git add rehketo/agent/run.py tests/integration/test_run_agent_approval.py
git commit -m "feat(api): run_agent pauses and resumes on tool approval interrupts"
```

---

### Task 8: Live-deps test pinning the REAL middleware contract

**Files:**
- Create: `rehketo-api/tests/integration/test_run_agent_approval_live.py`

The fake in Task 7 *emulates* the middleware contract; this test drives the real `create_deep_agent` graph (real HITL middleware, real postgres checkpointer, real interrupt/resume) with a scripted fake LLM — no Bifrost, no Anthropic. Marked `live_deps` like the canary; it is the spec's "pinned by an integration test against the real API".

- [ ] **Step 1: Write the test**

```python
"""Real-graph HITL canary: create_deep_agent + HumanInTheLoopMiddleware +
AsyncPostgresSaver, driven by a scripted tool-calling fake model. Pins the
interrupt payload shape and the Command(resume=...) format M3.5 relies on.
No LLM cost. Run explicitly:

    uv run pytest -m live_deps tests/integration/test_run_agent_approval_live.py
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import text

import rehketo.agent.graph as graph_mod
import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

pytestmark = pytest.mark.live_deps


class _ScriptedToolModel(GenericFakeChatModel):
    """GenericFakeChatModel that tolerates bind_tools (returns itself)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


async def _seed(db) -> Any:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="queued",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    db.add(
        McpServer(
            id=uuid4(),
            name="testsrv",
            url="https://unused.example.com/mcp",
            auth_token_ct=None,
            allowed_roles=["User"],
            enabled=True,
            auto_approve=False,
        )
    )
    await db.commit()
    return run.id


async def _event_types(run_id) -> list[str]:
    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    return [r.payload["type"] for r in rows]


async def _wait_for_status(run_id, status: str, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            async with sessionmaker()() as s:
                row = (
                    await s.execute(
                        text("SELECT status FROM runs WHERE id = :rid"),
                        {"rid": str(run_id)},
                    )
                ).one()
            if row.status == status:
                return
            await asyncio.sleep(0.1)


async def test_real_graph_pauses_and_resumes_on_approval(
    settings_env, db_url, db, monkeypatch
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    model = _ScriptedToolModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "testsrv__echo", "args": {"text": "hi"}, "id": "c1"}
                    ],
                ),
                AIMessage(content="the tool said hi back"),
            ]
        )
    )
    monkeypatch.setattr(graph_mod, "build_chat_model", lambda: model)

    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)
    task = asyncio.create_task(run_mod.run_agent(run_id, bus))

    await _wait_for_status(run_id, "pending_approval")
    # Find the REAL middleware's approval request and approve it.
    async with sessionmaker()() as s:
        payload = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.approval_required'"
                ),
                {"rid": str(run_id)},
            )
        ).one()
    assert payload.payload["tool"] == "testsrv__echo"
    assert payload.payload["arguments"] == {"text": "hi"}
    await bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": payload.payload["approval_id"],
            "decision": "approve",
        },
    )
    await asyncio.wait_for(task, timeout=60)

    types = await _event_types(run_id)
    # The approved tool actually executed: adapter events flowed as in M3.
    assert "tool.call" in types
    assert "tool.result" in types
    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
    assert row.status == "succeeded"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest -m live_deps tests/integration/test_run_agent_approval_live.py -q`
Expected: PASS. If it fails on the resume shape, the REAL middleware contract has drifted from the Task 6/7 assumption — fix `resolve_interrupt`'s `Command` construction to match what this test reveals, NOT the test.

- [ ] **Step 3: Verify it is skipped by default**

Run: `uv run pytest tests/integration/test_run_agent_approval_live.py -q`
Expected: deselected/skipped (the live_deps marker is excluded by default, same as the canary).

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_run_agent_approval_live.py
git commit -m "test(api): live-deps canary for real HITL interrupt/resume contract"
```

---

### Task 9: Sweep covers `pending_approval`

**Files:**
- Modify: `rehketo-api/rehketo/agent/sweep.py`
- Test: `rehketo-api/tests/integration/test_startup_sweep.py`

- [ ] **Step 1: Write the failing test**

Open `tests/integration/test_startup_sweep.py`, find how it seeds a run (it inserts a Run with status `running`/`queued` and calls `sweep_abandoned_runs`). Add a test in the same style:

```python
async def test_sweep_fails_pending_approval_runs(settings_env, db_url, db) -> None:
    # Mirror the seeding used by the existing tests in this file (reuse its
    # helper if one exists), but with status="pending_approval".
    ...
```

Concretely: copy the file's existing "running run gets swept" test body verbatim, change the seeded status to `"pending_approval"`, and assert the same outcome (status flips to `failed`, error code `process_restart`, `run.status` + `run.ended` events published).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_startup_sweep.py -q`
Expected: new test FAILS (run stays `pending_approval`)

- [ ] **Step 3: Implement**

In `rehketo/agent/sweep.py`, change the UPDATE's where clause:

```python
            .where(Run.status.in_(["queued", "running", "pending_approval"]))
```

and update the docstring's first line to: `"""On startup, mark any runs stuck in `running`, `queued`, or `pending_approval` as failed, ...` (a pending approval does not survive a restart — M3.5 scope decision; durable resume is M4).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_startup_sweep.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rehketo/agent/sweep.py tests/integration/test_startup_sweep.py
git commit -m "feat(api): startup sweep fails abandoned pending_approval runs"
```

---

### Task 10: Approval endpoint

**Files:**
- Modify: `rehketo-api/rehketo/api/runs.py`
- Test: `rehketo-api/tests/integration/test_run_approvals.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_run_approvals.py`:

```python
"""POST /runs/{run_id}/approvals/{approval_id} — guards and the happy path.
The endpoint validates (owner, pending status, known + undecided approval id)
then publishes tool.approval_decision to the durable bus; the waiting run
task picks it up from its own subscription (covered by
test_run_agent_approval.py)."""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Run, User, UserRole
from rehketo.main import create_app
from rehketo.runs.event_bus import PostgresEventBus


async def _seed_pending_run(db, *, role: str = "User") -> tuple[str, str, str, str]:
    """User + conversation + pending_approval run + one approval_required
    event. Returns (sid, csrf, run_id, approval_id)."""
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    if role:
        db.add(UserRole(user_id=u.id, role=role))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="pending_approval",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()
    approval_id = str(uuid4())
    await PostgresEventBus().publish(
        str(run.id),
        {
            "type": "tool.approval_required",
            "approval_id": approval_id,
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    csrf = issue_csrf_token(str(sid))
    return str(sid), csrf, str(run.id), approval_id


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


async def _decision_events(run_id: str) -> list[dict]:
    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.approval_decision' "
                    "ORDER BY sequence"
                ),
                {"rid": run_id},
            )
        ).all()
    return [r.payload for r in rows]


async def test_approve_publishes_decision_event(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 204
    events = await _decision_events(run_id)
    assert len(events) == 1
    assert events[0]["approval_id"] == approval_id
    assert events[0]["decision"] == "approve"


async def test_duplicate_decision_409(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "deny"},
            **_auth(sid, csrf),
        )
        r2 = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r1.status_code == 204
    assert r2.status_code == 409
    assert len(await _decision_events(run_id)) == 1  # first decision wins


async def test_unknown_approval_id_404(settings_env, db_url, db) -> None:
    sid, csrf, run_id, _approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{uuid4()}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 404


async def test_run_not_pending_409(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    async with sessionmaker()() as s:
        await s.execute(
            text("UPDATE runs SET status = 'running' WHERE id = :rid"),
            {"rid": run_id},
        )
        await s.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 409


async def test_other_users_run_404(settings_env, db_url, db) -> None:
    _sid, _csrf, run_id, approval_id = await _seed_pending_run(db)
    other = User(id=uuid4(), display_name="Eve", email=f"{uuid4()}@example.com")
    db.add(other)
    await db.flush()
    db.add(UserRole(user_id=other.id, role="User"))
    await db.commit()
    sid2 = await create_session(
        db,
        user_id=other.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf2 = issue_csrf_token(str(sid2))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "approve"},
            **_auth(str(sid2), csrf2),
        )
    assert r.status_code == 404


async def test_invalid_decision_value_422(settings_env, db_url, db) -> None:
    sid, csrf, run_id, approval_id = await _seed_pending_run(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/approvals/{approval_id}",
            json={"decision": "maybe"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_run_approvals.py -q`
Expected: FAIL — 404/405 (route does not exist)

- [ ] **Step 3: Implement the endpoint**

In `rehketo/api/runs.py`:

Add imports: `Literal` to the `typing` import, `RunEvent` to the models import (`from rehketo.db.models import Run, RunEvent`).

Add after the `cancel_run` handler:

```python
class ApprovalDecisionIn(BaseModel):
    decision: Literal["approve", "deny"]


@router.post("/{run_id}/approvals/{approval_id}", status_code=204)
async def decide_approval(
    run_id: UUID,
    approval_id: str,
    payload: ApprovalDecisionIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> None:
    """Record the user's decision for one pending tool call. The decision is
    published as a durable tool.approval_decision event; the waiting run task
    consumes it from its own bus subscription. First decision wins (409 on a
    repeat); validation is against run_events, the single source of truth."""
    perms.require(
        "chat.approve_tool_call",
        resource_type="run",
        resource_id=run_id,
    )
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status != "pending_approval":
        raise HTTPException(
            status_code=409, detail=f"run is {run.status}, not pending approval"
        )
    rows = (
        (
            await db.execute(
                select(RunEvent.payload).where(
                    RunEvent.run_id == run_id,
                    RunEvent.payload["type"].astext.in_(
                        ["tool.approval_required", "tool.approval_decision"]
                    ),
                    RunEvent.payload["approval_id"].astext == approval_id,
                )
            )
        )
        .scalars()
        .all()
    )
    types = {p["type"] for p in rows}
    if "tool.approval_required" not in types:
        raise HTTPException(status_code=404, detail="approval not found")
    if "tool.approval_decision" in types:
        raise HTTPException(status_code=409, detail="approval already decided")
    await request.app.state.event_bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": approval_id,
            "decision": payload.decision,
        },
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_run_approvals.py -q && uv run mypy rehketo && uv run bandit -r rehketo -q`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add rehketo/api/runs.py tests/integration/test_run_approvals.py
git commit -m "feat(api): POST /runs/{run_id}/approvals/{approval_id} endpoint"
```

---

### Task 11: Transcript reload — `ApprovalItem`

**Files:**
- Modify: `rehketo-api/rehketo/api/conversations.py`
- Test: `rehketo-api/tests/integration/test_conversation_transcript.py`

- [ ] **Step 1: Write the failing test**

Open `tests/integration/test_conversation_transcript.py` and follow its existing seeding helpers (it publishes `tool.call`/`tool.result` events and asserts the interleaved transcript). Add:

```python
async def test_transcript_includes_approval_items(settings_env, db_url, db) -> None:
    # Seed exactly like the existing tool-event test in this file (same
    # helper / fixtures), then publish an approval pair on the run's bus:
    #   {"type": "tool.approval_required", "approval_id": "ap-1",
    #    "tool": "testsrv__echo", "arguments": {"text": "hi"}}
    #   {"type": "tool.approval_decision", "approval_id": "ap-1",
    #    "decision": "deny"}
    # GET the conversation and assert:
    #   one item with kind == "approval", approval_id == "ap-1",
    #   tool == "testsrv__echo", arguments == {"text": "hi"}, decision == "deny"


async def test_transcript_pending_approval_has_null_decision(
    settings_env, db_url, db
) -> None:
    # Same seeding, publish ONLY the approval_required event.
    # Assert the approval item's decision is None (renders live buttons on
    # a pending_approval run).
```

Write both bodies concretely against the file's existing helpers — the assertions above are the complete required behavior; the seeding lines are whatever the sibling tool-event test already uses (reuse, don't reinvent).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/integration/test_conversation_transcript.py -q`
Expected: new tests FAIL (no `approval` kind in the union)

- [ ] **Step 3: Implement**

In `rehketo/api/conversations.py`:

After `ToolCallItem`, add:

```python
class ApprovalItem(BaseModel):
    """A tool-approval request reconstructed from run_events on reload.
    decision is None while undecided — on a pending_approval run the UI
    renders live approve/deny buttons from exactly this state."""

    kind: Literal["approval"] = "approval"
    run_id: UUID
    approval_id: str
    tool: str
    arguments: dict[str, object]
    decision: str | None = None
    created_at: datetime
```

Update the union:

```python
TranscriptItem = Annotated[
    MessageItem | ToolCallItem | ApprovalItem, Field(discriminator="kind")
]
```

After `_tool_items`, add the mirror helper:

```python
async def _approval_items(
    db: AsyncSession, conversation_id: UUID
) -> list[ApprovalItem]:
    """Reconstruct ApprovalItem entries from run_events, mirroring
    _tool_items: approval_required seeds, approval_decision (same run_id +
    approval_id) fills in the decision."""
    rows = (
        await db.execute(
            select(RunEvent.run_id, RunEvent.payload, RunEvent.created_at)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                Run.conversation_id == conversation_id,
                RunEvent.payload["type"].astext.in_(
                    ["tool.approval_required", "tool.approval_decision"]
                ),
            )
            .order_by(RunEvent.run_id, RunEvent.sequence)
        )
    ).all()
    by_approval_id: dict[tuple[UUID, str], ApprovalItem] = {}
    for run_id, payload, created_at in rows:
        approval_id = str(payload.get("approval_id", ""))
        key = (run_id, approval_id)
        if payload["type"] == "tool.approval_required":
            by_approval_id[key] = ApprovalItem(
                run_id=run_id,
                approval_id=approval_id,
                tool=str(payload.get("tool", "")),
                arguments=payload.get("arguments") or {},
                created_at=created_at,
            )
        elif key in by_approval_id:
            by_approval_id[key] = by_approval_id[key].model_copy(
                update={"decision": str(payload.get("decision", ""))}
            )
    return list(by_approval_id.values())
```

In `get_conversation`, next to the existing `tool_items` line, add:

```python
    approval_items: list[TranscriptItem] = list(await _approval_items(db, conv.id))
```

and include it in the sort:

```python
    items = sorted(
        message_items + tool_items + approval_items, key=lambda i: i.created_at
    )
```

Also update `active_run_id`'s in-flight query if it filters by status: run `grep -n "queued" rehketo/api/conversations.py` — if the active-run lookup uses `Run.status.in_(["queued", "running"])`, add `"pending_approval"` to that list (a paused run is still the conversation's live run; the UI must reattach to its stream).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_conversation_transcript.py tests/integration/test_conversations_detail.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rehketo/api/conversations.py tests/integration/test_conversation_transcript.py
git commit -m "feat(api): transcript reload reconstructs approval items from run_events"
```

---

### Task 12: OpenAPI rebaseline + full backend validation

**Files:**
- Modify: `rehketo-ui/openapi.snapshot.json` (generated)

- [ ] **Step 1: Rebaseline**

From `rehketo-api/`:
Run: `uv run python ../tools/check_contract.py --update`
Expected: `wrote rehketo-ui/openapi.snapshot.json`

- [ ] **Step 2: Run the full backend validation block (AGENTS.md)**

From `rehketo-api/`:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run python ../tools/check_contract.py
```

From repo root:

```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

Expected: ALL pass. Quote real output in the task report. Fix anything that fails before committing.

- [ ] **Step 3: Commit**

```bash
git add ../rehketo-ui/openapi.snapshot.json
git commit -m "chore(api): rebaseline OpenAPI snapshot for M3.5 endpoints"
```

---

### Task 13: UI types + SSE handlers

**Files:**
- Modify: `rehketo-ui/src/lib/types.ts`
- Modify: `rehketo-ui/src/lib/sse.ts`
- Test: `rehketo-ui/src/lib/sse.spec.ts`

All UI commands run from `rehketo-ui/`.

- [ ] **Step 1: Write the failing sse test**

Append inside the top-level `describe('subscribeRun', ...)` block in `src/lib/sse.spec.ts`, following the file's `MockEventSource` + handler-collection style:

```typescript
	test('dispatches approval events to handlers and keeps streaming through pending_approval', () => {
		const required: RunEvent[] = [];
		const decisions: RunEvent[] = [];
		const statuses: string[] = [];
		const sub = subscribeRun(
			'run-1',
			{
				onApprovalRequired: (e) => required.push(e),
				onApprovalDecision: (e) => decisions.push(e),
				onStatus: (status) => statuses.push(status)
			},
			{ EventSourceImpl: MockEventSource as unknown as new (url: string) => EventSource }
		);
		const source = MockEventSource.instances.at(-1)!;
		source.emitEvent({
			type: 'tool.approval_required',
			approval_id: 'ap-1',
			tool: 'testsrv__echo',
			arguments: { text: 'hi' },
			sequence: 1,
			run_id: 'run-1'
		});
		source.emitEvent({
			type: 'run.status',
			status: 'pending_approval',
			sequence: 2,
			run_id: 'run-1'
		});
		expect(sub.state).toBe('running'); // paused-for-approval is still a live stream
		source.emitEvent({
			type: 'tool.approval_decision',
			approval_id: 'ap-1',
			decision: 'approve',
			sequence: 3,
			run_id: 'run-1'
		});
		expect(required).toHaveLength(1);
		expect(required[0]).toMatchObject({ approval_id: 'ap-1', tool: 'testsrv__echo' });
		expect(decisions).toHaveLength(1);
		expect(statuses).toContain('pending_approval');
		sub.unsubscribe();
	});
```

- [ ] **Step 2: Run to verify failure**

Run: `pnpm run test:unit -- --run src/lib/sse.spec.ts`
Expected: FAIL — type errors (`tool.approval_required` not in RunEvent) and missing handlers

- [ ] **Step 3: Implement types**

In `src/lib/types.ts`:

- `RunStatus`:

```typescript
export type RunStatus =
	| 'queued'
	| 'running'
	| 'pending_approval'
	| 'succeeded'
	| 'failed'
	| 'cancelled';
```

- `Capability`: add `| 'chat.approve_tool_call'` after `'chat.use_mcp_server'`, and update the comment to `// The 12 canonical actions from rehketo-api/rehketo/permissions/actions.py.`
- `RunEvent` union — add two members after `tool.result`:

```typescript
	| {
			type: 'tool.approval_required';
			approval_id: string;
			tool: string;
			arguments: Record<string, unknown>;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'tool.approval_decision';
			approval_id: string;
			decision: 'approve' | 'deny';
			sequence: number;
			run_id: string;
	  };
```

- After `ToolCallItem`, add the transcript item and extend the union:

```typescript
export type ApprovalItem = {
	kind: 'approval';
	run_id: string;
	approval_id: string;
	tool: string;
	arguments: Record<string, unknown>;
	decision: 'approve' | 'deny' | null;
	created_at: string;
};

export type TranscriptItem = MessageItem | ToolCallItem | ApprovalItem;
```

- `McpServerOut`: add `auto_approve: boolean;` after `enabled`.

- [ ] **Step 4: Implement sse.ts**

In `src/lib/sse.ts`:

- `RunStreamHandlers` — add after `onToolResult`:

```typescript
	onApprovalRequired?: (event: Extract<RunEvent, { type: 'tool.approval_required' }>) => void;
	onApprovalDecision?: (event: Extract<RunEvent, { type: 'tool.approval_decision' }>) => void;
```

- In `connect`, after the `tool.result` listener, add:

```typescript
		self.addEventListener('tool.approval_required', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'tool.approval_required' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onApprovalRequired?.(event);
		});

		self.addEventListener('tool.approval_decision', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'tool.approval_decision' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onApprovalDecision?.(event);
		});
```

- In the `run.status` listener's status ladder, add after the `running` branch:

```typescript
				} else if (event.status === 'pending_approval') {
					// Paused for a user decision — the stream stays live.
					sub.state = 'running';
```

- Update the protocol comment block at the top of the file: after the `tool.call / tool.result` line, add:

```
// tool.approval_required pauses the run (run.status=pending_approval)
// until a tool.approval_decision arrives; the stream stays open throughout.
```

- [ ] **Step 5: Run tests**

Run: `pnpm run test:unit -- --run src/lib/sse.spec.ts && pnpm run check`
Expected: PASS / clean. `pnpm run check` will surface every site that switches exhaustively on `RunStatus` or `TranscriptItem` — fix each by handling the new variants (the ChatView/MessageList handling lands in Task 14; if check fails only there, proceed to Task 14 and run check again before committing both together — otherwise commit now).

- [ ] **Step 6: Commit** (if `pnpm run check` is clean)

```bash
git add src/lib/types.ts src/lib/sse.ts src/lib/sse.spec.ts
git commit -m "feat(ui): approval events in types and run stream"
```

---

### Task 14: ApprovalCard + transcript wiring

**Files:**
- Create: `rehketo-ui/src/lib/components/ApprovalCard.svelte`
- Create: `rehketo-ui/src/lib/components/ApprovalCard.dom.spec.ts`
- Modify: `rehketo-ui/src/lib/components/MessageList.svelte`
- Modify: `rehketo-ui/src/lib/components/ChatView.svelte`

- [ ] **Step 1: Write the failing component test**

Create `src/lib/components/ApprovalCard.dom.spec.ts` (same mount/unmount style as `ToolChip.dom.spec.ts`):

```typescript
import { mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import ApprovalCard from './ApprovalCard.svelte';
import type { ApprovalItem } from '$lib/types';

function item(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
	return {
		kind: 'approval',
		run_id: 'run-1',
		approval_id: 'ap-1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		decision: null,
		created_at: '2026-06-12T00:00:00Z',
		...overrides
	};
}

describe('ApprovalCard', () => {
	it('renders pending state with approve/deny buttons when decidable', () => {
		const onDecide = vi.fn();
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item(), canDecide: true, onDecide }
		});
		expect(document.body.textContent).toContain('testsrv__echo');
		expect(document.querySelector('[data-decision="pending"]')).not.toBeNull();
		(document.querySelector('[data-action="approve"]') as HTMLButtonElement).click();
		expect(onDecide).toHaveBeenCalledWith('approve');
		(document.querySelector('[data-action="deny"]') as HTMLButtonElement).click();
		expect(onDecide).toHaveBeenCalledWith('deny');
		unmount(app);
		document.body.innerHTML = '';
	});

	it('hides buttons without decide capability', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item(), canDecide: false }
		});
		expect(document.querySelector('[data-action="approve"]')).toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('renders approved state without buttons', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item({ decision: 'approve' }), canDecide: true }
		});
		expect(document.querySelector('[data-decision="approve"]')).not.toBeNull();
		expect(document.querySelector('[data-action="approve"]')).toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('renders denied state', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item({ decision: 'deny' }), canDecide: true }
		});
		expect(document.querySelector('[data-decision="deny"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});
```

- [ ] **Step 2: Run to verify failure**

Run: `pnpm run test:unit -- --run src/lib/components/ApprovalCard.dom.spec.ts`
Expected: FAIL — component does not exist

- [ ] **Step 3: Create the component**

`src/lib/components/ApprovalCard.svelte` (dark-workbench styling matching ToolChip; no new design language):

```svelte
<script lang="ts">
	import type { ApprovalItem } from '$lib/types';

	let {
		item,
		canDecide = false,
		onDecide
	}: {
		item: ApprovalItem;
		canDecide?: boolean;
		onDecide?: (decision: 'approve' | 'deny') => void;
	} = $props();
</script>

<div
	class="rounded-md border border-accent/40 bg-surface/60 text-xs"
	data-decision={item.decision ?? 'pending'}
>
	<div class="flex items-center gap-2 px-3 py-1.5 text-muted">
		{#if item.decision === null}
			<span
				class="h-2 w-2 animate-pulse rounded-full bg-accent"
				role="img"
				aria-label="awaiting approval"
			></span>
		{:else if item.decision === 'approve'}
			<span role="img" aria-label="approved">✓</span>
		{:else}
			<span class="text-danger" role="img" aria-label="denied">✗</span>
		{/if}
		<span class="font-mono">{item.tool}</span>
		<span>requests approval</span>
	</div>
	<pre
		class="overflow-x-auto whitespace-pre-wrap border-t border-border px-3 py-2">{JSON.stringify(
			item.arguments,
			null,
			2
		)}</pre>
	{#if item.decision === null && canDecide}
		<div class="flex gap-2 border-t border-border px-3 py-2">
			<button
				type="button"
				data-action="approve"
				onclick={() => onDecide?.('approve')}
				class="rounded-md bg-accent px-2 py-1 text-xs font-semibold text-white"
			>
				Approve
			</button>
			<button
				type="button"
				data-action="deny"
				onclick={() => onDecide?.('deny')}
				class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
			>
				Deny
			</button>
		</div>
	{/if}
</div>
```

- [ ] **Step 4: Run component test**

Run: `pnpm run test:unit -- --run src/lib/components/ApprovalCard.dom.spec.ts`
Expected: PASS

- [ ] **Step 5: Wire MessageList**

In `src/lib/components/MessageList.svelte`:

- Import: `import ApprovalCard from './ApprovalCard.svelte';` and add `ApprovalItem` to the types import.
- Props gain decide plumbing:

```typescript
	let {
		items,
		liveRunId = null,
		streamingText = null,
		streamingStatus = null,
		canDecide = false,
		onDecide
	}: {
		items: TranscriptItem[];
		liveRunId?: string | null;
		streamingText?: string | null;
		streamingStatus?: RunStatus | null;
		canDecide?: boolean;
		onDecide?: (item: ApprovalItem, decision: 'approve' | 'deny') => void;
	} = $props();
```

- The `#each` key handles three kinds:

```svelte
	{#each items as item (item.kind === 'message'
		? item.id
		: item.kind === 'tool'
			? `${item.run_id}:${item.call_id}`
			: `${item.run_id}:${item.approval_id}`)}
```

- The item branch gains:

```svelte
				{#if item.kind === 'message'}
					<MessageBubble message={item} />
				{:else if item.kind === 'tool'}
					<ToolChip {item} live={item.run_id === liveRunId} />
				{:else}
					<ApprovalCard
						{item}
						canDecide={canDecide && item.run_id === liveRunId}
						onDecide={(decision) => onDecide?.(item, decision)}
					/>
				{/if}
```

- The waiting caption, inside the `showStreamingBubble` block after `<AssistantBubble ... />`:

```svelte
				{#if streamingStatus === 'pending_approval'}
					<p class="mt-1 text-xs text-muted">Waiting for tool approval…</p>
				{/if}
```

(`isActivelyStreaming` stays as-is: `pending_approval` is not in its list, so the bubble renders static markdown while paused — correct, the pause can be long.)

- [ ] **Step 6: Wire ChatView**

In `src/lib/components/ChatView.svelte`:

- Add `ApprovalItem` to the types import.
- In the `subscribeRun` handlers, after `onToolResult`:

```typescript
				onApprovalRequired: (event) => {
					if (
						!items.some(
							(i) =>
								i.kind === 'approval' &&
								i.run_id === event.run_id &&
								i.approval_id === event.approval_id
						)
					) {
						items = [
							...items,
							{
								kind: 'approval',
								run_id: event.run_id,
								approval_id: event.approval_id,
								tool: event.tool,
								arguments: event.arguments,
								decision: null,
								created_at: new Date(Date.now()).toISOString()
							}
						];
					}
				},
				onApprovalDecision: (event) => {
					// Resolve on the EVENT, not the POST response — a second tab
					// (or another device) resolves the same card this way.
					items = items.map((i) =>
						i.kind === 'approval' && i.approval_id === event.approval_id
							? { ...i, decision: event.decision }
							: i
					);
				},
```

- Add the decide function next to `cancelActiveRun`:

```typescript
	async function decideApproval(
		item: ApprovalItem,
		decision: 'approve' | 'deny'
	): Promise<void> {
		try {
			await apiFetch(`/runs/${item.run_id}/approvals/${item.approval_id}`, {
				method: 'POST',
				body: JSON.stringify({ decision })
			});
		} catch (err) {
			// 409 = already decided (other tab) or run no longer pending; the
			// decision event (or terminal status) updates the card — swallow.
			if (err instanceof ApiError && err.status === 409) return;
			if (err instanceof ApiError) console.warn('approval failed:', err.code, err.message);
		}
	}
```

- Pass the plumbing to MessageList:

```svelte
	<MessageList
		{items}
		liveRunId={activeRunId}
		{streamingText}
		{streamingStatus}
		canDecide={auth.can('chat.approve_tool_call')}
		onDecide={decideApproval}
	/>
```

- [ ] **Step 7: Run all UI checks**

Run: `pnpm run test:unit -- --run && pnpm run check && pnpm run lint`
Expected: PASS / clean. If `ChatView.dom.spec.ts` mounts ChatView with a full conversation fixture, TypeScript may require nothing new (the items array accepts the wider union); fix any surfaced exhaustiveness errors.

- [ ] **Step 8: Commit**

```bash
git add src/lib/components/ApprovalCard.svelte src/lib/components/ApprovalCard.dom.spec.ts src/lib/components/MessageList.svelte src/lib/components/ChatView.svelte src/lib/types.ts src/lib/sse.ts src/lib/sse.spec.ts
git commit -m "feat(ui): approval card, transcript wiring, decide flow"
```

(Include types.ts/sse.ts here if Task 13 deferred its commit pending `pnpm run check`.)

---

### Task 15: Admin page `auto_approve` toggle

**Files:**
- Modify: `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte`
- Test: `rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts`

- [ ] **Step 1: Write the failing test**

Open `page.dom.spec.ts` and follow its existing mocking pattern (it mounts the page with `data` props and stubs `apiFetch`). Add two tests in that style:

1. **Create sends auto_approve:** fill the form, check the new `#mcp-auto-approve` checkbox, click create; assert the mocked `apiFetch` POST body includes `"auto_approve":true`.
2. **Row toggle PATCHes auto_approve:** render with one server (`auto_approve: false` — remember the `McpServerOut` fixture in this spec file needs the new field), click `[data-action="toggle-auto-approve"]`; assert PATCH body is `{"auto_approve":true}`.

Use the file's existing fixture/mock helpers verbatim; the only novel content is the two interactions above.

- [ ] **Step 2: Run to verify failure**

Run: `pnpm run test:unit -- --run "src/routes/(app)/settings/mcp-servers/page.dom.spec.ts"`
Expected: FAIL

- [ ] **Step 3: Implement**

In `+page.svelte`:

- State: `let autoApprove = $state(false);` next to the other form fields; reset it to `false` in `create()`'s post-success block.
- `create()` body gains `auto_approve: autoApprove,` in the POST JSON.
- Toggle function next to `toggle`:

```typescript
	async function toggleAutoApprove(server: McpServerOut): Promise<void> {
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ auto_approve: !server.auto_approve })
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}
```

- Row metadata line gains the badge — extend the roles line:

```svelte
							<p class="text-xs text-muted">
								roles: {server.allowed_roles.join(', ')}{#if server.has_auth_token}&nbsp;· token set{/if}{#if server.auto_approve}&nbsp;· auto-approve{/if}
							</p>
```

- Row buttons gain (before the Delete button):

```svelte
							<button
								type="button"
								data-action="toggle-auto-approve"
								onclick={() => toggleAutoApprove(server)}
								class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
							>
								{server.auto_approve ? 'Require approval' : 'Auto-approve'}
							</button>
```

- Create form gains (after the roles fieldset):

```svelte
				<label class="flex items-center gap-2 text-sm">
					<input id="mcp-auto-approve" type="checkbox" bind:checked={autoApprove} />
					Auto-approve tool calls (trusted server — skips per-call user approval)
				</label>
```

- Update the page intro paragraph to mention approval:

```svelte
	<p class="mt-1 text-sm text-muted">
		External tool servers available to agent runs. Granted roles get all of a server's tools;
		tool calls require per-call user approval unless auto-approve is on. Disable to take a
		server offline without deleting it.
	</p>
```

- [ ] **Step 4: Run UI validation**

Run: `pnpm run test:unit -- --run && pnpm run check && pnpm run lint`
Expected: PASS / clean

- [ ] **Step 5: Commit**

```bash
git add "src/routes/(app)/settings/mcp-servers/+page.svelte" "src/routes/(app)/settings/mcp-servers/page.dom.spec.ts"
git commit -m "feat(ui): auto_approve toggle on mcp server admin page"
```

---

### Task 16: Final validation + roadmap note

**Files:**
- Modify: `docs/superpowers/specs/2026-06-10-roadmap-family-launch-design.md` (only if marking shipped is requested — otherwise skip; the spec reference already landed with the spec commit)

- [ ] **Step 1: Full backend validation (from `rehketo-api/`)**

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run python ../tools/check_contract.py
uv run pytest -m live_deps tests/integration/test_run_agent_approval_live.py
```

- [ ] **Step 2: Full UI validation (from `rehketo-ui/`)**

```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

- [ ] **Step 3: Repo guards (from root)**

```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

- [ ] **Step 4: Quote all real output in the completion report** (charter rule 5). Any failure goes back to its task — do not "done" past a red check.

---

## Self-review notes (already applied)

- **Spec coverage:** schema → T1; admin API → T3; interrupt wiring → T4+T5; resume loop → T7; decision transport/endpoint → T10 (+T6 waiter); SSE events → T6/T10 (publish sites) + T13 (types); sweep → T9; cancel-while-pending → T7 test; transcript reload → T11; UI card/reload/indicator → T14; admin toggle → T15; contract → T12; middleware contract pin → T8; validation → T16.
- **Type consistency:** `build_run_toolset` returns `tuple[list[StructuredTool], dict[str, InterruptOnConfig]]` (T5) and `run.py` unpacks exactly that (T5/T7). Wire decision values are `approve|deny` everywhere (endpoint, events, UI); `reject` appears ONLY inside `resolve_interrupt`'s middleware mapping and the middleware-emulating fakes.
- **Known judgment calls for the executor:** Task 11's transcript tests intentionally reference the sibling test's seeding helpers rather than duplicating unknown fixture code — reuse what the file already has; the assertions listed are the complete required behavior. Task 15's spec additions likewise follow that file's existing mock harness.
