# MCP Tool Calling (M3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rehketo becomes an MCP host: admin-configured HTTP MCP servers feed tools into agent runs, tool activity streams over SSE and survives reload, gated by a per-server per-role allowlist through the single permission gate.

**Architecture:** A new `mcp_servers` table (admin CRUD + admin UI page) holds server config. A new `rehketo/mcp/` package connects one fastmcp `Client` per allowed server at run start, adapts MCP tools to LangChain `StructuredTool`s whose coroutines publish `tool.call`/`tool.result` to the durable event bus, and hands them to `build_agent(tools=...)`. The conversation transcript endpoint reconstructs tool activity from `run_events` on reload. Spec: `docs/superpowers/specs/2026-06-11-mcp-tool-calling-design.md`.

**Tech Stack:** FastAPI + async SQLAlchemy/psycopg3 + Alembic; fastmcp (`Client`, `StreamableHttpTransport`); langchain-core `StructuredTool` (accepts JSON-schema dict as `args_schema` since 1.4); SvelteKit 5 runes + vitest.

**Conventions that bind every task:** Conventional Commits, NO AI attribution trailers (enforced hook). All Python fully typed (`disallow_untyped_defs`). Logging only via `rehketo.core.logging.get_logger`. Settings only via `rehketo.config`. Every permission call threads `resource_id`. Run API commands from `rehketo-api/`, UI commands from `rehketo-ui/`.

**Plan-discovered design note (amends spec, documented in Task 15):** LangGraph executes parallel tool calls concurrently, and tool coroutines publish to the bus while `run_agent`'s stream loop also publishes deltas. `PostgresEventBus.publish` computes `MAX(sequence)+1` per run, so concurrent publishes for the same run can collide on the `(run_id, sequence)` unique constraint. Task 2 serializes publishes with a process-local per-run `asyncio.Lock` (both publishers live in the same process — also true after the M4 worker split, where both move together).

---

## File map

**rehketo-api (create):**
- `alembic/versions/0011_mcp_servers.py` — migration
- `rehketo/api/mcp_servers.py` — admin CRUD router
- `rehketo/mcp/__init__.py` — package marker
- `rehketo/mcp/servers.py` — load enabled servers, filter via permission gate
- `rehketo/mcp/adapter.py` — MCP tool → StructuredTool with event publishing
- `rehketo/mcp/registry.py` — per-run client lifecycle + toolset assembly
- `tests/integration/test_event_bus_concurrent_publish.py`
- `tests/integration/test_mcp_servers_admin.py`
- `tests/integration/test_mcp_allowed_servers.py`
- `tests/unit/test_mcp_adapter.py`
- `tests/integration/test_mcp_registry.py`
- `tests/integration/test_run_agent_tools.py`
- `tests/integration/test_conversation_transcript.py`

**rehketo-api (modify):**
- `rehketo/permissions/actions.py` — two new actions
- `rehketo/permissions/roles.py` — grants
- `rehketo/permissions/check.py` — `resource_roles` keyword
- `rehketo/permissions/dependencies.py` — thread `resource_roles`
- `rehketo/runs/event_bus.py` — per-run publish lock
- `rehketo/db/models.py` — `McpServer` model
- `rehketo/agent/graph.py` — `tools` parameter
- `rehketo/agent/run.py` — build toolset, pass tools, close clients
- `rehketo/api/conversations.py` — transcript items union
- `rehketo/main.py` — include admin router
- `.importlinter` — contracts for `rehketo.mcp`
- `pyproject.toml` — fastmcp + mcp dependencies
- `tests/unit/test_permissions_check.py` — gate tests
- `../rehketo-ui/openapi.snapshot.json` — rebaselined (Tasks 5 and 11)

**rehketo-ui (create):**
- `src/lib/components/ToolChip.svelte` + `src/lib/components/ToolChip.dom.spec.ts`
- `src/routes/(app)/settings/mcp-servers/+page.ts`, `+page.svelte`, `page.dom.spec.ts`

**rehketo-ui (modify):**
- `src/lib/types.ts` — RunEvent/Capability/transcript/admin types
- `src/lib/sse.ts` + `src/lib/sse.spec.ts` — tool event handlers
- `src/lib/components/MessageList.svelte`, `ChatView.svelte`, `ChatView.dom.spec.ts` — items-based transcript
- `src/routes/(app)/settings/+page.svelte` — link to MCP servers page
- `src/routes/(app)/c/[id]/+page.ts` (only if it narrows `ConversationDetail`; check during Task 13)

**docs (modify, Task 15):**
- `docs/superpowers/specs/2026-06-10-roadmap-family-launch-design.md` — M3.5, per-tool follow-up
- `docs/superpowers/specs/2026-06-11-mcp-tool-calling-design.md` — publish-lock amendment

---

### Task 1: Permission gate — new actions and `resource_roles`

**Files:**
- Modify: `rehketo-api/rehketo/permissions/actions.py`
- Modify: `rehketo-api/rehketo/permissions/roles.py`
- Modify: `rehketo-api/rehketo/permissions/check.py`
- Modify: `rehketo-api/rehketo/permissions/dependencies.py`
- Test: `rehketo-api/tests/unit/test_permissions_check.py` (extend existing file)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_permissions_check.py`:

```python
def test_use_mcp_server_requires_role_intersection() -> None:
    assert check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=["User", "Admin"],
    )
    assert not check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=["Admin"],
    )
    assert not check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=[],
    )


def test_resource_roles_does_not_bypass_action_grant() -> None:
    # A role named in resource_roles still needs the action itself.
    assert not check_permission(
        ["Guest"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id=None,
        resource_roles=["Guest"],
    )


def test_existing_actions_unaffected_by_default() -> None:
    assert check_permission(
        ["User"], "chat.write", resource_type="conversation", resource_id=None
    )


def test_admin_manage_mcp_servers_is_admin_only() -> None:
    assert check_permission(
        ["Admin"], "admin.manage_mcp_servers", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["User"], "admin.manage_mcp_servers", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["Moderator"],
        "admin.manage_mcp_servers",
        resource_type=None,
        resource_id=None,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `rehketo-api/`): `uv run pytest tests/unit/test_permissions_check.py -v`
Expected: FAIL — `PermissionError: unknown action: 'chat.use_mcp_server'` and `TypeError: ... unexpected keyword argument 'resource_roles'`

- [ ] **Step 3: Implement**

`actions.py` — add to the tuple (keep domains grouped):

```python
ACTIONS: tuple[str, ...] = (
    # Chat domain
    "chat.create_conversation",
    "chat.view_conversation",
    "chat.rename_conversation",
    "chat.delete_conversation",
    "chat.write",
    "chat.cancel_run",
    "chat.upload_files",
    "chat.use_mcp_server",
    # Admin domain
    "admin.manage_users",
    "admin.view_audit",
    "admin.manage_mcp_servers",
)
```

`roles.py` — add `"chat.use_mcp_server"` to BOTH the `Moderator` and `User` frozensets (Admin gets everything via `frozenset(ACTIONS)`).

`check.py` — replace `check_permission` with:

```python
def check_permission(
    roles: Iterable[str],
    action: str,
    *,
    resource_type: str | None,
    resource_id: UUID | str | None,
    resource_roles: Iterable[str] | None = None,
) -> bool:
    """
    Returns True iff the caller is allowed to perform `action` on the
    given resource. `resource_type` and `resource_id` are accepted now;
    v1 RBAC ignores them. Do not remove them from call sites.

    `resource_roles` is the per-resource role allowlist (today: an MCP
    server's allowed_roles). When provided, the caller must hold the
    action AND share at least one role with the allowlist. At the OpenFGA
    cutover this becomes a relationship check; only this body changes.
    """
    if action not in ACTIONS_SET:
        raise PermissionError(f"unknown action: {action!r}")
    caller_roles = set(roles)
    if action not in permissions_for_roles(caller_roles):
        return False
    if resource_roles is not None:
        return bool(caller_roles & set(resource_roles))
    return True
```

`dependencies.py` — thread the new keyword through both methods:

```python
    def can(
        self,
        action: str,
        *,
        resource_type: str | None = None,
        resource_id: UUID | str | None = None,
        resource_roles: Iterable[str] | None = None,
    ) -> bool:
        return check_permission(
            self.roles,
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_roles=resource_roles,
        )

    def require(
        self,
        action: str,
        *,
        resource_type: str | None = None,
        resource_id: UUID | str | None = None,
        resource_roles: Iterable[str] | None = None,
    ) -> None:
        if not self.can(
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_roles=resource_roles,
        ):
            raise HTTPException(status_code=403, detail=f"denied: {action}")
```

Add `from collections.abc import Iterable` to the `TYPE_CHECKING` block in `dependencies.py` (it already exists in `check.py`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_permissions_check.py -v`
Expected: PASS (all, including pre-existing tests)

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/permissions tests/unit/test_permissions_check.py
git commit -m "feat(permissions): mcp actions and resource_roles allowlist in the gate"
```

---

### Task 2: Event bus — serialize concurrent publishers per run

**Files:**
- Modify: `rehketo-api/rehketo/runs/event_bus.py`
- Test: `rehketo-api/tests/integration/test_event_bus_concurrent_publish.py`

Concurrent tool coroutines + the delta stream loop publish to the same run; `MAX(sequence)+1` races trip the `(run_id, sequence)` unique constraint. Fix: process-local per-run `asyncio.Lock`.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_event_bus_concurrent_publish.py`:

```python
from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import text

from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Run, User
from rehketo.runs.event_bus import PostgresEventBus


async def _seed_run(db) -> str:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    conv = Conversation(id=uuid4(), user_id=u.id)
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="running",
        model="claude-sonnet-4-6",
    )
    db.add_all([u, conv, run])
    await db.commit()
    return str(run.id)


async def test_concurrent_publishes_get_distinct_sequences(
    settings_env, db_url, db
) -> None:
    run_id = await _seed_run(db)
    bus = PostgresEventBus()

    await asyncio.gather(
        *(
            bus.publish(run_id, {"type": "tool.call", "call_id": f"c{i}"})
            for i in range(20)
        )
    )

    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT sequence FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": run_id},
            )
        ).all()
    assert [r.sequence for r in rows] == list(range(20))


async def test_lock_is_dropped_after_run_ended(settings_env, db_url, db) -> None:
    run_id = await _seed_run(db)
    bus = PostgresEventBus()
    await bus.publish(run_id, {"type": "run.status", "status": "running"})
    assert run_id in bus._publish_locks
    await bus.publish(run_id, {"type": "run.ended"})
    assert run_id not in bus._publish_locks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_event_bus_concurrent_publish.py -v`
Expected: FAIL — `test_concurrent_publishes_get_distinct_sequences` raises `IntegrityError` (unique violation on `(run_id, sequence)`) or asserts a gap; `test_lock_is_dropped_after_run_ended` fails with `AttributeError: _publish_locks`

(If the first test happens to pass by timing luck, the second still fails — the lock structure does not exist yet.)

- [ ] **Step 3: Implement**

In `event_bus.py`, add to `PostgresEventBus.__init__`:

```python
        # publish() computes MAX(sequence)+1 per run; concurrent publishers
        # for the same run (parallel tool calls + the delta stream loop) race
        # that read. All of a run's publishers live in this process — true
        # today and after the M4 worker split — so a process-local per-run
        # lock is sufficient. Popped on run.ended (the guaranteed terminator).
        self._publish_locks: dict[str, asyncio.Lock] = {}
```

Replace `publish` with:

```python
    async def publish(self, run_id: str, event: dict[str, object]) -> None:
        lock = self._publish_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            async with sessionmaker()() as db:
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
        if event.get("type") == "run.ended":
            self._publish_locks.pop(run_id, None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_event_bus_concurrent_publish.py tests/integration/ -k "event_bus or run_agent" -v`
Expected: PASS (new tests and all pre-existing bus/run tests)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/runs/event_bus.py tests/integration/test_event_bus_concurrent_publish.py
git commit -m "fix(runs): serialize per-run event publishes for concurrent tool calls"
```

---

### Task 3: `mcp_servers` migration + model

**Files:**
- Create: `rehketo-api/alembic/versions/0011_mcp_servers.py`
- Modify: `rehketo-api/rehketo/db/models.py`
- Test: covered by Task 5's integration tests (the `db_url` fixture runs all migrations); this task verifies via alembic round-trip

- [ ] **Step 1: Write the migration**

Create `alembic/versions/0011_mcp_servers.py`:

```python
"""admin-configured MCP servers

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11 00:00:00.000000+00:00

Server list is data, never code (north star): rows are managed live from
the admin API, no restart to reconfigure. auth_token_ct follows the
sessions.refresh_token_ct pattern — Fernet ciphertext bytes, never
returned by the API. allowed_roles is JSONB-on-the-row because roles are
plain strings in ROLE_PERMISSIONS, not DB entities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("auth_token_ct", sa.LargeBinary(), nullable=True),
        sa.Column("allowed_roles", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
```

- [ ] **Step 2: Add the model**

In `rehketo/db/models.py`, add `Boolean` to the `sqlalchemy` import list, then append after `UserPreferences`:

```python
class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_token_ct: Mapped[bytes | None] = mapped_column(LargeBinary)
    allowed_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 3: Verify the migration round-trips**

Requires the local dev postgres (`just db` from the repo root if not running). Run from `rehketo-api/`:

```bash
uv run alembic upgrade head && uv run alembic downgrade 0010 && uv run alembic upgrade head
```
Expected: three clean runs, last line `Running upgrade 0010 -> 0011, admin-configured MCP servers`

- [ ] **Step 4: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add alembic/versions/0011_mcp_servers.py rehketo/db/models.py
git commit -m "feat(db): mcp_servers table and model"
```

---

### Task 4: Add fastmcp dependency

**Files:**
- Modify: `rehketo-api/pyproject.toml` (+ `uv.lock`)

- [ ] **Step 1: Add the dependencies**

From `rehketo-api/`:

```bash
uv add fastmcp mcp
```

(`mcp` is fastmcp's protocol-layer dependency; we import `mcp.types.Tool`/`TextContent` in the adapter, so declare it explicitly rather than ride transitively.)

- [ ] **Step 2: Verify the import surface this plan uses**

```bash
uv run python -c "
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import TextContent, Tool
print('ok')
"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(api): add fastmcp and mcp dependencies"
```

---

### Task 5: Admin CRUD routes for MCP servers

**Files:**
- Create: `rehketo-api/rehketo/api/mcp_servers.py`
- Modify: `rehketo-api/rehketo/main.py` (router include)
- Modify: `rehketo-ui/openapi.snapshot.json` (rebaseline)
- Test: `rehketo-api/tests/integration/test_mcp_servers_admin.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_mcp_servers_admin.py`:

```python
from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import McpServer, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "Admin") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add_all([u, UserRole(user_id=u.id, role=role)])
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(sid), issue_csrf_token(str(sid))


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


_CREATE_BODY = {
    "name": "github",
    "url": "https://mcp.example.com/mcp",
    "auth_token": "secret-token",
    "allowed_roles": ["Admin", "User"],
    "enabled": True,
}


async def test_crud_roundtrip_token_write_only(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        created = r.json()
        assert created["name"] == "github"
        assert created["has_auth_token"] is True
        assert "auth_token" not in created
        server_id = created["id"]

        r = await c.get("/admin/mcp-servers", cookies={SESSION_COOKIE: sid})
        assert r.status_code == 200
        assert [s["id"] for s in r.json()["items"]] == [server_id]

        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"enabled": False, "auth_token": None},
            **_auth(sid, csrf),
        )
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["has_auth_token"] is False

        # PATCH that omits auth_token leaves it unchanged.
        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"auth_token": "tok2"},
            **_auth(sid, csrf),
        )
        assert r.json()["has_auth_token"] is True
        r = await c.patch(
            f"/admin/mcp-servers/{server_id}",
            json={"enabled": True},
            **_auth(sid, csrf),
        )
        assert r.json()["has_auth_token"] is True

        r = await c.delete(f"/admin/mcp-servers/{server_id}", **_auth(sid, csrf))
        assert r.status_code == 204

    row = (
        await db.execute(select(McpServer).where(McpServer.id == server_id))
    ).scalar_one_or_none()
    assert row is None


async def test_token_is_encrypted_at_rest(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
    row = (
        await db.execute(select(McpServer).where(McpServer.name == "github"))
    ).scalar_one()
    assert row.auth_token_ct is not None
    assert b"secret-token" not in row.auth_token_ct


async def test_non_admin_is_403(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db, role="User")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/admin/mcp-servers", cookies={SESSION_COOKIE: sid})
        assert r.status_code == 403
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 403


async def test_duplicate_name_is_409(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        r = await c.post("/admin/mcp-servers", json=_CREATE_BODY, **_auth(sid, csrf))
        assert r.status_code == 409


async def test_bad_url_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/mcp-servers",
            json={**_CREATE_BODY, "url": "not-a-url"},
            **_auth(sid, csrf),
        )
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_mcp_servers_admin.py -v`
Expected: FAIL — all tests 404 (router not registered)

- [ ] **Step 3: Implement the router**

Create `rehketo/api/mcp_servers.py`:

```python
from __future__ import annotations

from datetime import datetime  # noqa: TC003  # pydantic field at runtime
from typing import Annotated
from uuid import (
    UUID,  # noqa: TC003  # pydantic fields and path params at runtime
    uuid4,
)

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.auth.crypto import encrypt_token
from rehketo.db import get_session
from rehketo.db.models import McpServer
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(prefix="/admin/mcp-servers", tags=["admin"])

# Slug-like: the name prefixes tool names ({name}__{tool}) on the model's
# tool list, so keep it identifier-safe and stable.
_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class McpServerCreate(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN)
    url: HttpUrl
    auth_token: str | None = None
    allowed_roles: list[str]
    enabled: bool = True


class McpServerPatch(BaseModel):
    # name is identity (tool prefix, unique key) — not patchable; recreate
    # instead. auth_token: absent = unchanged, null = clear (distinguished
    # via model_fields_set).
    url: HttpUrl | None = None
    auth_token: str | None = None
    allowed_roles: list[str] | None = None
    enabled: bool | None = None


class McpServerOut(BaseModel):
    id: UUID
    name: str
    url: str
    has_auth_token: bool
    allowed_roles: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class McpServerList(BaseModel):
    items: list[McpServerOut]


def _to_out(s: McpServer) -> McpServerOut:
    return McpServerOut(
        id=s.id,
        name=s.name,
        url=s.url,
        has_auth_token=s.auth_token_ct is not None,
        allowed_roles=s.allowed_roles,
        enabled=s.enabled,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _get_or_404(db: AsyncSession, server_id: UUID) -> McpServer:
    server = (
        await db.execute(select(McpServer).where(McpServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.get("", response_model=McpServerList)
async def list_servers(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerList:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=None
    )
    rows = (
        (await db.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    )
    return McpServerList(items=[_to_out(s) for s in rows])


@router.post("", status_code=201, response_model=McpServerOut)
async def create_server(
    payload: McpServerCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerOut:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=None
    )
    dup = (
        await db.execute(select(McpServer.id).where(McpServer.name == payload.name))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="server name already exists")
    server = McpServer(
        id=uuid4(),
        name=payload.name,
        url=str(payload.url),
        auth_token_ct=(
            encrypt_token(payload.auth_token) if payload.auth_token else None
        ),
        allowed_roles=payload.allowed_roles,
        enabled=payload.enabled,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return _to_out(server)


@router.patch("/{server_id}", response_model=McpServerOut)
async def patch_server(
    server_id: UUID,
    payload: McpServerPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerOut:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=server_id
    )
    server = await _get_or_404(db, server_id)
    if payload.url is not None:
        server.url = str(payload.url)
    if "auth_token" in payload.model_fields_set:
        server.auth_token_ct = (
            encrypt_token(payload.auth_token) if payload.auth_token else None
        )
    if payload.allowed_roles is not None:
        server.allowed_roles = payload.allowed_roles
    if payload.enabled is not None:
        server.enabled = payload.enabled
    server.updated_at = func.now()
    await db.commit()
    await db.refresh(server)
    return _to_out(server)


@router.delete("/{server_id}", status_code=204)
async def delete_server(
    server_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=server_id
    )
    server = await _get_or_404(db, server_id)
    await db.delete(server)
    await db.commit()
    return Response(status_code=204)
```

In `rehketo/main.py`, inside `create_app()` add to the imports-and-includes block:

```python
    from rehketo.api import mcp_servers as mcp_servers_api
```
and
```python
    app.include_router(mcp_servers_api.router)
```
(keep both lists alphabetical: after `me`, before `messages`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_servers_admin.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Rebaseline the OpenAPI snapshot**

```bash
uv run python ../tools/check_contract.py --update
uv run python ../tools/check_contract.py
```
Expected: second command exits 0 (`OpenAPI snapshot matches` or equivalent success output)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo
git add rehketo/api/mcp_servers.py rehketo/main.py tests/integration/test_mcp_servers_admin.py ../rehketo-ui/openapi.snapshot.json
git commit -m "feat(api): admin CRUD for MCP servers"
```

---

### Task 6: `rehketo/mcp/servers.py` — allowed servers for a role set

**Files:**
- Create: `rehketo-api/rehketo/mcp/__init__.py`
- Create: `rehketo-api/rehketo/mcp/servers.py`
- Test: `rehketo-api/tests/integration/test_mcp_allowed_servers.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_mcp_allowed_servers.py`:

```python
from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import McpServer
from rehketo.mcp.servers import allowed_servers


def _server(name: str, roles: list[str], *, enabled: bool = True) -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=f"https://{name}.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=roles,
        enabled=enabled,
    )


async def test_filters_by_role_and_enabled(settings_env, db_url, db) -> None:
    db.add_all(
        [
            _server("everyone", ["Admin", "Moderator", "User"]),
            _server("admins-only", ["Admin"]),
            _server("disabled", ["User"], enabled=False),
        ]
    )
    await db.commit()

    user_servers = await allowed_servers(db, ["User"])
    assert [s.name for s in user_servers] == ["everyone"]

    admin_servers = await allowed_servers(db, ["Admin"])
    assert [s.name for s in admin_servers] == ["admins-only", "everyone"]


async def test_no_roles_means_no_servers(settings_env, db_url, db) -> None:
    db.add(_server("everyone", ["Admin", "Moderator", "User"]))
    await db.commit()
    assert await allowed_servers(db, []) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_mcp_allowed_servers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.mcp'`

- [ ] **Step 3: Implement**

Create `rehketo/mcp/__init__.py` (empty file).

Create `rehketo/mcp/servers.py`:

```python
"""Which MCP servers a run may use. The single permission gate decides:
chat.use_mcp_server + the server row's allowed_roles as resource_roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from rehketo.db.models import McpServer
from rehketo.permissions.check import check_permission

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession


async def allowed_servers(
    db: AsyncSession, roles: Iterable[str]
) -> list[McpServer]:
    caller_roles = list(roles)
    rows = (
        (
            await db.execute(
                select(McpServer)
                .where(McpServer.enabled.is_(True))
                .order_by(McpServer.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        s
        for s in rows
        if check_permission(
            caller_roles,
            "chat.use_mcp_server",
            resource_type="mcp_server",
            resource_id=s.id,
            resource_roles=s.allowed_roles,
        )
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_mcp_allowed_servers.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/mcp tests/integration/test_mcp_allowed_servers.py
git commit -m "feat(mcp): allowed_servers filters via the permission gate"
```

---

### Task 7: `rehketo/mcp/adapter.py` — MCP tool → StructuredTool with events

**Files:**
- Create: `rehketo-api/rehketo/mcp/adapter.py`
- Test: `rehketo-api/tests/unit/test_mcp_adapter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_mcp_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from fastmcp.client.client import CallToolResult
from mcp.types import TextContent, Tool

from rehketo.mcp.adapter import (
    RESULT_EVENT_MAX_CHARS,
    build_structured_tool,
)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append((run_id, event))


class FakeClient:
    def __init__(self, *, text: str = "ok", is_error: bool = False,
                 raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._text = text
        self._is_error = is_error
        self._raise = raise_exc

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, raise_on_error: bool = True
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        return CallToolResult(
            content=[TextContent(type="text", text=self._text)],
            structured_content=None,
            meta=None,
            is_error=self._is_error,
        )


def _tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text back.",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


async def test_success_publishes_call_then_result() -> None:
    bus, client = FakeBus(), FakeClient(text="echo: hi")
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    assert tool.name == "testsrv__echo"

    result = await tool.ainvoke({"text": "hi"})

    assert result == "echo: hi"
    assert client.calls == [("echo", {"text": "hi"})]
    types = [e["type"] for _, e in bus.events]
    assert types == ["tool.call", "tool.result"]
    call_event, result_event = bus.events[0][1], bus.events[1][1]
    assert call_event["tool"] == "testsrv__echo"
    assert call_event["arguments"] == {"text": "hi"}
    assert result_event["call_id"] == call_event["call_id"]
    assert result_event["result"] == "echo: hi"
    assert result_event["is_error"] is False


async def test_mcp_error_result_sets_is_error() -> None:
    bus = FakeBus()
    client = FakeClient(text="boom", is_error=True)
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert result == "boom"
    assert bus.events[1][1]["is_error"] is True


async def test_transport_exception_becomes_error_result() -> None:
    bus = FakeBus()
    client = FakeClient(raise_exc=RuntimeError("connection lost"))
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert "connection lost" in result
    assert bus.events[1][1]["is_error"] is True


async def test_result_event_is_truncated_but_return_is_not() -> None:
    big = "x" * (RESULT_EVENT_MAX_CHARS + 1000)
    bus, client = FakeBus(), FakeClient(text=big)
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert result == big  # full text goes back to the model
    event_result = bus.events[1][1]["result"]
    assert len(event_result) < len(big)
    assert "truncated" in event_result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mcp_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.mcp.adapter'`

- [ ] **Step 3: Implement**

Create `rehketo/mcp/adapter.py`:

```python
"""Convert one MCP tool into a LangChain StructuredTool.

The wrapper coroutine is where tool.call / tool.result events are born —
published straight to the durable bus, never parsed out of LangGraph's
message stream, so the SSE schema stays decoupled from LangGraph internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.tools import StructuredTool
from mcp.types import TextContent

from rehketo.core.logging import format_exc_for_log, get_logger

if TYPE_CHECKING:
    from fastmcp import Client
    from fastmcp.client.client import CallToolResult
    from mcp.types import Tool

    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

# Cap the result payload in the *event* (bus + UI protection). The full
# text still returns to the model.
RESULT_EVENT_MAX_CHARS = 16_384
_TRUNCATION_MARKER = "\n…[truncated for event stream]"


def _result_text(result: CallToolResult) -> str:
    parts = [
        block.text for block in result.content if isinstance(block, TextContent)
    ]
    return "\n".join(parts)


def _truncate_for_event(text: str) -> str:
    if len(text) <= RESULT_EVENT_MAX_CHARS:
        return text
    return text[:RESULT_EVENT_MAX_CHARS] + _TRUNCATION_MARKER


def build_structured_tool(
    *,
    server_name: str,
    tool: Tool,
    client: Client,  # type: ignore[type-arg]  # transport generic is irrelevant here
    run_id: str,
    bus: RunEventBus,
) -> StructuredTool:
    full_name = f"{server_name}__{tool.name}"

    async def _invoke(**kwargs: Any) -> str:
        call_id = str(uuid4())
        await bus.publish(
            run_id,
            {
                "type": "tool.call",
                "call_id": call_id,
                "tool": full_name,
                "arguments": kwargs,
            },
        )
        try:
            result = await client.call_tool(tool.name, kwargs, raise_on_error=False)
            text = _result_text(result)
            is_error = result.is_error
        except Exception as exc:
            # Transport/protocol failure mid-call. The error text goes back
            # to the model as the tool result so the agent can recover.
            logger.warning(
                "tool call failed server=%s tool=%s: %s",
                server_name,
                tool.name,
                format_exc_for_log(exc),
            )
            text = f"tool call failed: {exc}"
            is_error = True
        await bus.publish(
            run_id,
            {
                "type": "tool.result",
                "call_id": call_id,
                "result": _truncate_for_event(text),
                "is_error": is_error,
            },
        )
        return text

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=full_name,
        description=tool.description or "",
        # langchain-core ≥1.4 accepts a JSON-schema dict directly — the MCP
        # inputSchema passes through with no pydantic model generation.
        args_schema=tool.inputSchema,
    )
```

Note: if `format_exc_for_log` lives elsewhere than `rehketo.core.logging`, check `rehketo/core/logging.py` and import from the actual location (AGENTS.md names it as the sanctioned exception formatter).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mcp_adapter.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/mcp/adapter.py tests/unit/test_mcp_adapter.py
git commit -m "feat(mcp): adapt MCP tools to StructuredTools that publish run events"
```

---

### Task 8: `rehketo/mcp/registry.py` — per-run toolset assembly

**Files:**
- Create: `rehketo-api/rehketo/mcp/registry.py`
- Test: `rehketo-api/tests/integration/test_mcp_registry.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_mcp_registry.py`:

```python
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

from fastmcp import Client, FastMCP

from rehketo.db.models import McpServer
from rehketo.mcp import registry


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append((run_id, event))


def _echo_server() -> FastMCP:
    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    return server


def _row(name: str, url: str = "https://unused.example.com/mcp") -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=url,
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )


async def test_builds_tools_from_reachable_server(
    settings_env, monkeypatch
) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=bus
        )
        assert [t.name for t in tools] == ["testsrv__echo"]
        result = await tools[0].ainvoke({"text": "hi"})

    assert result == "echo: hi"
    assert [e["type"] for _, e in bus.events] == ["tool.call", "tool.result"]


async def test_unreachable_server_is_skipped(settings_env, monkeypatch) -> None:
    good = _echo_server()

    def _client_for(server: McpServer) -> Client:
        if server.name == "bad":
            raise RuntimeError("refused")
        return Client(good)

    monkeypatch.setattr(registry, "_client_for", _client_for)

    async with AsyncExitStack() as stack:
        tools = await registry.build_run_toolset(
            stack, [_row("bad"), _row("testsrv")], run_id="r1", bus=FakeBus()
        )
        assert [t.name for t in tools] == ["testsrv__echo"]


async def test_no_servers_yields_no_tools(settings_env) -> None:
    async with AsyncExitStack() as stack:
        assert await registry.build_run_toolset(
            stack, [], run_id="r1", bus=FakeBus()
        ) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_mcp_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.mcp.registry'`

- [ ] **Step 3: Implement**

Create `rehketo/mcp/registry.py`:

```python
"""Per-run MCP client lifecycle: connect to each allowed server, list its
tools, adapt them. Connections are per-run (opened by run_agent, closed via
the caller's AsyncExitStack) — no shared state across requests or processes,
the property M1 established and the M4 worker split depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from rehketo.auth.crypto import decrypt_token
from rehketo.core.logging import format_exc_for_log, get_logger
from rehketo.mcp.adapter import build_structured_tool

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AsyncExitStack

    from langchain_core.tools import StructuredTool

    from rehketo.db.models import McpServer
    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)


def _client_for(server: McpServer) -> Client:  # type: ignore[type-arg]  # transport generic is irrelevant here
    """Client construction seam — tests monkeypatch this to inject an
    in-memory FastMCP transport."""
    token = decrypt_token(server.auth_token_ct) if server.auth_token_ct else None
    return Client(StreamableHttpTransport(server.url, auth=token))


async def build_run_toolset(
    stack: AsyncExitStack,
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> list[StructuredTool]:
    tools: list[StructuredTool] = []
    for server in servers:
        try:
            client = _client_for(server)
            await stack.enter_async_context(client)
            mcp_tools = await client.list_tools()
        except Exception as exc:
            # A broken tool server must not take chat down: skip it, the
            # run proceeds with the remaining tools (spec: error handling).
            logger.warning(
                "mcp server %s unavailable, skipping: %s",
                server.name,
                format_exc_for_log(exc),
            )
            continue
        tools.extend(
            build_structured_tool(
                server_name=server.name,
                tool=t,
                client=client,
                run_id=run_id,
                bus=bus,
            )
            for t in mcp_tools
        )
    return tools
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_mcp_registry.py tests/unit/test_mcp_adapter.py -v`
Expected: PASS

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo
git add rehketo/mcp/registry.py tests/integration/test_mcp_registry.py
git commit -m "feat(mcp): per-run toolset registry with skip-on-failure"
```

---

### Task 9: Wire tools into `build_agent` and `run_agent`

**Files:**
- Modify: `rehketo-api/rehketo/agent/graph.py`
- Modify: `rehketo-api/rehketo/agent/run.py`
- Test: `rehketo-api/tests/integration/test_run_agent_tools.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_run_agent_tools.py`. It follows `test_run_agent_end_to_end.py`'s pattern (fake `build_agent` patched in `run.py`'s namespace to bypass Bifrost) — but the fake now receives the `tools` kwarg and *invokes the first tool* mid-stream, proving run.py built the registry, the adapter published events through the real bus, and cleanup ran:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus


async def _seed(db, *, role: str = "User") -> tuple[Any, Any]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    conv = Conversation(id=uuid4(), user_id=u.id)
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="queued",
        model="claude-sonnet-4-6",
    )
    db.add_all(
        [
            u,
            UserRole(user_id=u.id, role=role),
            conv,
            run,
            McpServer(
                id=uuid4(),
                name="testsrv",
                url="https://unused.example.com/mcp",
                auth_token_ct=None,
                allowed_roles=[role],
                enabled=True,
            ),
        ]
    )
    await db.commit()
    return run.id, conv.id


async def test_run_agent_executes_tools_and_streams_events(
    settings_env, db_url, db, monkeypatch
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    captured: dict[str, Any] = {}

    class _ToolCallingAgent:
        def __init__(self, tools: Sequence[Any]) -> None:
            self._tools = tools

        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            # Simulate the model deciding to call the tool mid-stream.
            await self._tools[0].ainvoke({"text": "hi"})
            yield (AIMessageChunk(content="done", id="msg-fake-1"), {})

    async def _fake_build_agent(
        run_id: str, system_prompt: str, tools: Sequence[Any] = ()
    ) -> AsyncIterator[_ToolCallingAgent]:
        captured["tools"] = list(tools)
        yield _ToolCallingAgent(tools)

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id, _conv_id = await _seed(db)
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)

    assert [t.name for t in captured["tools"]] == ["testsrv__echo"]

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
    types = [r.payload["type"] for r in rows]
    assert "tool.call" in types
    assert "tool.result" in types
    assert types.index("tool.call") < types.index("tool.result")
    assert types[-1] == "run.ended"
    result_event = next(
        r.payload for r in rows if r.payload["type"] == "tool.result"
    )
    assert result_event["result"] == "echo: hi"
    assert result_event["is_error"] is False


async def test_user_without_server_role_gets_no_tools(
    settings_env, db_url, db, monkeypatch
) -> None:
    captured: dict[str, Any] = {}

    class _QuietAgent:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            yield (AIMessageChunk(content="hello", id="msg-fake-2"), {})

    async def _fake_build_agent(
        run_id: str, system_prompt: str, tools: Sequence[Any] = ()
    ) -> AsyncIterator[_QuietAgent]:
        captured["tools"] = list(tools)
        yield _QuietAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id, _conv_id = await _seed(db, role="Moderator")
    # Server allows only Moderator; flip the seeded user's role to User by
    # seeding a different role on the server row instead: simplest is a
    # second seed with a role the server does not allow.
    async with sessionmaker()() as s:
        await s.execute(
            text("UPDATE mcp_servers SET allowed_roles = '[\"Admin\"]'::jsonb")
        )
        await s.commit()

    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)
    assert captured["tools"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_run_agent_tools.py -v`
Expected: FAIL — `TypeError: _fake_build_agent() ... 'tools'` mismatch is masked by the fake; the real failure is `captured["tools"]` asserting `["testsrv__echo"] != []` because run.py never builds a toolset yet. (The fake accepts `tools` with a default, so the call itself succeeds.)

- [ ] **Step 3: Implement — `graph.py`**

`build_agent` gains a `tools` parameter (stays a pure function of its inputs):

```python
async def build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[BaseTool] = (),
) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. The system prompt is assembled by the caller
    (rehketo.agent.prompt); tools are assembled by the caller
    (rehketo.mcp.registry) — graph construction stays a pure function of its
    inputs. The graph is a LangGraph CompiledStateGraph; deepagents accepts
    `checkpointer=` as a constructor kwarg (verified against the real API).
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=list(tools),
            system_prompt=system_prompt,
            model=build_chat_model(),
            checkpointer=saver,
        )
        yield agent
```

Add to the `TYPE_CHECKING` block: `from collections.abc import Sequence` and `from langchain_core.tools import BaseTool`. (FastAPI is not involved here — TYPE_CHECKING-only imports are fine since `from __future__ import annotations` is present.)

- [ ] **Step 4: Implement — `run.py`**

Add imports:

```python
from contextlib import AsyncExitStack

from rehketo.db.models import Conversation, Message, Run, UserPreferences, UserRole
from rehketo.mcp.registry import build_run_toolset
from rehketo.mcp.servers import allowed_servers
```

(`contextlib` is already imported for `suppress`; use `contextlib.AsyncExitStack` or import it — match the file's existing `import contextlib` style and write `contextlib.AsyncExitStack()`.)

In the inner `try`, extend the existing DB block that loads history + preferences to also load roles + servers, then wrap the agent loop in an exit stack. The block becomes:

```python
            async with sessionmaker()() as db:
                await db.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status="running",
                        started_at=datetime.now(UTC),
                    )
                )
                await db.commit()
                await bus.publish(
                    str(run_id), {"type": "run.status", "status": "running"}
                )

                history = await _load_history(db, conversation_id)
                prefs = (
                    await db.execute(
                        select(UserPreferences).where(
                            UserPreferences.user_id == user_id
                        )
                    )
                ).scalar_one_or_none()
                custom_instructions = prefs.custom_instructions if prefs else None
                roles = (
                    (
                        await db.execute(
                            select(UserRole.role).where(UserRole.user_id == user_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                servers = await allowed_servers(db, roles)
            system_prompt = assemble_system_prompt(custom_instructions)

            # MCP clients live exactly as long as the agent run; the exit
            # stack closes them on every path (success, failure, cancel).
            async with contextlib.AsyncExitStack() as stack:
                tools = await build_run_toolset(
                    stack, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id), system_prompt, tools=tools
                ):
                    async for chunk in agent.astream(
                        {"messages": history},
                        config={"configurable": {"thread_id": str(run_id)}},
                        stream_mode="messages",
                    ):
                        for event in transform_chunk(chunk):  # type: ignore[arg-type]
                            await bus.publish(str(run_id), event)
                            if event["type"] == "message.delta":
                                assembled_text += str(event["delta"])
```

Everything after (persist message, statuses, title) is unchanged and stays OUTSIDE the exit stack. If ruff `PLR0915` (max statements) trips on `run_agent`, the existing `# noqa: PLR0915` on the def line already covers it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_run_agent_tools.py tests/integration/ -k "run_agent" -v`
Expected: PASS (new tests and the pre-existing end-to-end test — its fake `build_agent` signature must keep working; if it fails on the new kwarg, update that fake to accept `tools: Sequence[Any] = ()` as part of this task)

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/agent/graph.py rehketo/agent/run.py tests/integration/test_run_agent_tools.py tests/integration/test_run_agent_end_to_end.py
git commit -m "feat(agent): runs carry MCP tools from the registry"
```

---

### Task 10: import-linter contracts for `rehketo.mcp`

**Files:**
- Modify: `rehketo-api/.importlinter`

- [ ] **Step 1: Add contracts**

Append to `.importlinter`:

```ini
[importlinter:contract:mcp-layering]
name = mcp depends only on db, permissions, runs, auth crypto, core, config
type = forbidden
source_modules =
    rehketo.mcp
forbidden_modules =
    rehketo.api
    rehketo.agent

[importlinter:contract:api-never-imports-mcp]
name = api does not import mcp (admin routes touch only db + permissions)
type = forbidden
source_modules =
    rehketo.api
forbidden_modules =
    rehketo.mcp
```

- [ ] **Step 2: Verify**

Run: `uv run lint-imports`
Expected: `Contracts: N kept, 0 broken.` (N = existing 3 + 2 new)

- [ ] **Step 3: Commit**

```bash
git add .importlinter
git commit -m "chore(api): import-linter contracts for rehketo.mcp"
```

---

### Task 11: Transcript reload — tool activity from `run_events`

**Files:**
- Modify: `rehketo-api/rehketo/api/conversations.py`
- Modify: `rehketo-ui/openapi.snapshot.json` (rebaseline)
- Test: `rehketo-api/tests/integration/test_conversation_transcript.py`

`ConversationDetail.messages` becomes `items` — a discriminated union of message and tool entries, chronologically interleaved. Breaking change, no shim (charter: v1 changes behavior outright). The UI catches up in Task 13; UI pre-commit hooks don't run on API-only commits.

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_conversation_transcript.py`:

```python
from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import SESSION_COOKIE
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, Message, Run, User, UserRole
from rehketo.main import create_app
from rehketo.runs.event_bus import PostgresEventBus


async def test_items_interleave_tool_activity(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    conv = Conversation(id=uuid4(), user_id=u.id)
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="claude-sonnet-4-6",
    )
    db.add_all([u, UserRole(user_id=u.id, role="User"), conv, run])
    await db.commit()

    # User message, then tool events (durable bus), then assistant message —
    # the natural order of a tool-using run.
    db.add(
        Message(
            id=uuid4(),
            conversation_id=conv.id,
            role="user",
            content={"text": "hi"},
        )
    )
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {
            "type": "tool.call",
            "call_id": "c1",
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )
    await bus.publish(
        str(run.id),
        {
            "type": "tool.result",
            "call_id": "c1",
            "result": "echo: hi",
            "is_error": False,
        },
    )

    db.add(
        Message(
            id=uuid4(),
            conversation_id=conv.id,
            role="assistant",
            content={"text": "done"},
            run_id=run.id,
        )
    )
    await db.commit()

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
    assert r.status_code == 200
    items = r.json()["items"]
    kinds = [(i["kind"], i.get("role") or i.get("tool")) for i in items]
    assert kinds == [
        ("message", "user"),
        ("tool", "testsrv__echo"),
        ("message", "assistant"),
    ]
    tool_item = items[1]
    assert tool_item["call_id"] == "c1"
    assert tool_item["arguments"] == {"text": "hi"}
    assert tool_item["result"] == "echo: hi"
    assert tool_item["is_error"] is False
    assert tool_item["run_id"] == str(run.id)


async def test_call_without_result_is_pending(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    conv = Conversation(id=uuid4(), user_id=u.id)
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="failed",
        model="claude-sonnet-4-6",
    )
    db.add_all([u, UserRole(user_id=u.id, role="User"), conv, run])
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {"type": "tool.call", "call_id": "c1", "tool": "t__x", "arguments": {}},
    )

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
    items = r.json()["items"]
    assert items[0]["kind"] == "tool"
    assert items[0]["result"] is None
    assert items[0]["is_error"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_conversation_transcript.py -v`
Expected: FAIL — `KeyError: 'items'` (response still has `messages`)

- [ ] **Step 3: Implement**

In `rehketo/api/conversations.py`:

Add imports: `from typing import Annotated, Literal`, `from pydantic import BaseModel, Field`, and add `RunEvent` to the models import.

Add models after `MessageOut`:

```python
class MessageItem(MessageOut):
    kind: Literal["message"] = "message"


class ToolCallItem(BaseModel):
    """A tool invocation reconstructed from run_events on reload — the event
    log is the single source of truth for live streaming, resume, and
    transcript history. result is None while no tool.result event exists
    (in-flight, or the run died mid-call)."""

    kind: Literal["tool"] = "tool"
    run_id: UUID
    call_id: str
    tool: str
    arguments: dict[str, object]
    result: str | None = None
    is_error: bool | None = None
    created_at: datetime


TranscriptItem = Annotated[MessageItem | ToolCallItem, Field(discriminator="kind")]
```

Change `ConversationDetail`:

```python
class ConversationDetail(ConversationSummary):
    # Chronologically interleaved transcript: messages + tool activity.
    items: list[TranscriptItem]
    # In-flight run for this conversation (queued/running), newest first.
    # (existing comment block unchanged)
    active_run_id: UUID | None = None
```

Add a helper above `get_conversation`:

```python
async def _tool_items(db: AsyncSession, conversation_id: UUID) -> list[ToolCallItem]:
    rows = (
        await db.execute(
            select(RunEvent.run_id, RunEvent.payload, RunEvent.created_at)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                Run.conversation_id == conversation_id,
                RunEvent.payload["type"].astext.in_(["tool.call", "tool.result"]),
            )
            .order_by(RunEvent.run_id, RunEvent.sequence)
        )
    ).all()
    by_call_id: dict[str, ToolCallItem] = {}
    for run_id, payload, created_at in rows:
        call_id = str(payload.get("call_id", ""))
        if payload["type"] == "tool.call":
            by_call_id[call_id] = ToolCallItem(
                run_id=run_id,
                call_id=call_id,
                tool=str(payload.get("tool", "")),
                arguments=payload.get("arguments") or {},
                created_at=created_at,
            )
        elif call_id in by_call_id:
            item = by_call_id[call_id]
            by_call_id[call_id] = item.model_copy(
                update={
                    "result": str(payload.get("result", "")),
                    "is_error": bool(payload.get("is_error", False)),
                }
            )
    return list(by_call_id.values())
```

In `get_conversation`, replace the `messages=[...]` construction in the return with interleaving:

```python
    message_items: list[TranscriptItem] = [
        MessageItem(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            run_id=m.run_id,
            created_at=m.created_at,
            run_status=run_status if run_status in terminal else None,
            run_error=run_error if run_status in terminal else None,
        )
        for m, run_status, run_error in rows
    ]
    tool_items: list[TranscriptItem] = list(await _tool_items(db, conv.id))
    items = sorted(message_items + tool_items, key=lambda i: i.created_at)
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        active_run_id=active_run_id,
        items=items,
    )
```

(`sorted` is stable; a tool.call always commits before the run's assistant message is inserted, so timestamps order correctly.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_conversation_transcript.py tests/integration/ -k "conversation" -v`
Expected: PASS for new tests. Any pre-existing conversation tests that assert on `messages` must be updated in this task to read `items` and filter `kind == "message"` — update assertions, not behavior.

- [ ] **Step 5: Rebaseline the OpenAPI snapshot**

```bash
uv run python ../tools/check_contract.py --update
uv run python ../tools/check_contract.py
```
Expected: second command exits 0

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format && uv run ruff check && uv run mypy rehketo
git add rehketo/api/conversations.py tests/integration/ ../rehketo-ui/openapi.snapshot.json
git commit -m "feat(api): conversation transcript interleaves tool activity from run_events"
```

---

### Task 12: UI — event types and SSE tool handlers (additive, non-breaking)

**Files:**
- Modify: `rehketo-ui/src/lib/types.ts`
- Modify: `rehketo-ui/src/lib/sse.ts`
- Test: `rehketo-ui/src/lib/sse.spec.ts` (extend)

This task is purely additive — `ConversationDetail` is NOT changed here (that breaking change rides with the component rework in Task 13 so every commit keeps `pnpm run check` green).

- [ ] **Step 1: Write the failing test**

Append to the `describe('subscribeRun', ...)` block in `src/lib/sse.spec.ts` (uses the existing `MockEventSource` / `collectHandlers` helpers; extend `collectHandlers` with two arrays and handlers following the exact pattern of `deltas`/`completes`):

In `collectHandlers`, add to the returned object and handler wiring:

```typescript
        toolCalls: { tool: string; call_id: string }[];
        toolResults: { call_id: string; is_error: boolean }[];
```
with collection arrays and:
```typescript
                    onToolCall: (e) => toolCalls.push({ tool: e.tool, call_id: e.call_id }),
                    onToolResult: (e) => toolResults.push({ call_id: e.call_id, is_error: e.is_error })
```

New test:

```typescript
	test('tool flow: tool.call and tool.result reach handlers and track sequence', () => {
		const c = collectHandlers();
		subscribeRun('run-1', c.handlers, {
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		const src = MockEventSource.instances[0]!;

		src.emitEvent({
			type: 'tool.call',
			call_id: 'c1',
			tool: 'testsrv__echo',
			arguments: { text: 'hi' },
			sequence: 1,
			run_id: 'run-1'
		});
		src.emitEvent({
			type: 'tool.result',
			call_id: 'c1',
			result: 'echo: hi',
			is_error: false,
			sequence: 2,
			run_id: 'run-1'
		});

		expect(c.toolCalls).toEqual([{ tool: 'testsrv__echo', call_id: 'c1' }]);
		expect(c.toolResults).toEqual([{ call_id: 'c1', is_error: false }]);
	});
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `rehketo-ui/`): `pnpm run test:unit -- --run src/lib/sse.spec.ts`
Expected: FAIL — TypeScript: `'tool.call'` is not assignable to `RunEvent['type']` (and unknown handler props)

- [ ] **Step 3: Implement — `types.ts`**

Add the two actions to the `Capability` union (and bump the count in its comment from 9 to 11):

```typescript
	| 'chat.use_mcp_server'
	| 'admin.manage_mcp_servers';
```

Add two members to the `RunEvent` union:

```typescript
	| {
			type: 'tool.call';
			call_id: string;
			tool: string;
			arguments: Record<string, unknown>;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'tool.result';
			call_id: string;
			result: string;
			is_error: boolean;
			sequence: number;
			run_id: string;
	  }
```

Also add (used by Tasks 13–14; harmless now):

```typescript
// Transcript items — matches rehketo-api/rehketo/api/conversations.py
// MessageItem / ToolCallItem discriminated union.
export type MessageItem = MessageOut & { kind: 'message' };

export type ToolCallItem = {
	kind: 'tool';
	run_id: string;
	call_id: string;
	tool: string;
	arguments: Record<string, unknown>;
	result: string | null;
	is_error: boolean | null;
	created_at: string;
};

export type TranscriptItem = MessageItem | ToolCallItem;

// Matches rehketo-api/rehketo/api/mcp_servers.py McpServerOut.
export type McpServerOut = {
	id: string;
	name: string;
	url: string;
	has_auth_token: boolean;
	allowed_roles: string[];
	enabled: boolean;
	created_at: string;
	updated_at: string;
};

export type McpServerList = {
	items: McpServerOut[];
};
```

(Do NOT change `ConversationDetail` yet — Task 13.)

- [ ] **Step 4: Implement — `sse.ts`**

Add to `RunStreamHandlers`:

```typescript
	onToolCall?: (event: Extract<RunEvent, { type: 'tool.call' }>) => void;
	onToolResult?: (event: Extract<RunEvent, { type: 'tool.result' }>) => void;
```

In `connect()`, add two listeners alongside the existing ones (tool events also prove liveness — `track` refills the reconnect budget):

```typescript
		self.addEventListener('tool.call', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'tool.call' }>>(evt);
			if (!event) return;
			track(event);
			if (sub.state === 'idle' || sub.state === 'queued') sub.state = 'running';
			handlers.onToolCall?.(event);
		});

		self.addEventListener('tool.result', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'tool.result' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onToolResult?.(event);
		});
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pnpm run test:unit -- --run src/lib/sse.spec.ts && pnpm run check`
Expected: PASS, 0 svelte-check errors

- [ ] **Step 6: Commit**

```bash
git add src/lib/types.ts src/lib/sse.ts src/lib/sse.spec.ts
git commit -m "feat(ui): tool.call/tool.result stream events and contract types"
```

---

### Task 13: UI — items-based transcript with tool chips

**Files:**
- Create: `rehketo-ui/src/lib/components/ToolChip.svelte`
- Create: `rehketo-ui/src/lib/components/ToolChip.dom.spec.ts`
- Modify: `rehketo-ui/src/lib/types.ts` (`ConversationDetail`)
- Modify: `rehketo-ui/src/lib/components/MessageList.svelte`
- Modify: `rehketo-ui/src/lib/components/ChatView.svelte`
- Modify: `rehketo-ui/src/lib/components/ChatView.dom.spec.ts` (fixture)
- Check: `rehketo-ui/src/routes/(app)/c/[id]/+page.ts` — update only if it references `.messages`

- [ ] **Step 1: Write the failing tests**

Create `src/lib/components/ToolChip.dom.spec.ts`:

```typescript
import { mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';

import ToolChip from './ToolChip.svelte';
import type { ToolCallItem } from '$lib/types';

function item(overrides: Partial<ToolCallItem> = {}): ToolCallItem {
	return {
		kind: 'tool',
		run_id: 'run-1',
		call_id: 'c1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		result: null,
		is_error: null,
		created_at: '2026-06-11T00:00:00Z',
		...overrides
	};
}

describe('ToolChip', () => {
	it('shows running state while no result', () => {
		const app = mount(ToolChip, { target: document.body, props: { item: item() } });
		expect(document.body.textContent).toContain('testsrv__echo');
		expect(document.querySelector('[data-status="running"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('shows success state and result when expanded', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item({ result: 'echo: hi', is_error: false }) }
		});
		expect(document.querySelector('[data-status="done"]')).not.toBeNull();
		expect(document.body.textContent).toContain('echo: hi');
		unmount(app);
		document.body.innerHTML = '';
	});

	it('shows error state when is_error', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item({ result: 'boom', is_error: true }) }
		});
		expect(document.querySelector('[data-status="error"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});
```

In `src/lib/components/ChatView.dom.spec.ts`, update the `conversation()` fixture: `messages: []` becomes `items: []`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pnpm run test:unit -- --run src/lib/components`
Expected: FAIL — cannot resolve `./ToolChip.svelte`; ChatView spec fails on the fixture type

- [ ] **Step 3: Implement — `types.ts` breaking change**

```typescript
export type ConversationDetail = ConversationSummary & {
	items: TranscriptItem[];
	active_run_id: string | null;
};
```

- [ ] **Step 4: Implement — `ToolChip.svelte`**

```svelte
<script lang="ts">
	import type { ToolCallItem } from '$lib/types';

	let { item }: { item: ToolCallItem } = $props();

	let status = $derived(
		item.result === null && item.is_error === null
			? 'running'
			: item.is_error
				? 'error'
				: 'done'
	);
</script>

<details
	class="rounded-md border border-border bg-surface/60 text-xs"
	data-status={status}
>
	<summary class="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-muted">
		{#if status === 'running'}
			<span class="size-2 animate-pulse rounded-full bg-accent" aria-label="running"></span>
		{:else if status === 'error'}
			<span class="text-danger" aria-label="failed">✗</span>
		{:else}
			<span class="text-muted" aria-label="succeeded">✓</span>
		{/if}
		<span class="font-mono">{item.tool}</span>
	</summary>
	<div class="space-y-2 border-t border-border px-3 py-2">
		<pre class="overflow-x-auto whitespace-pre-wrap">{JSON.stringify(item.arguments, null, 2)}</pre>
		{#if item.result !== null}
			<pre class="overflow-x-auto whitespace-pre-wrap text-muted">{item.result}</pre>
		{/if}
	</div>
</details>
```

- [ ] **Step 5: Implement — `MessageList.svelte`**

Switch the prop from `messages: MessageOut[]` to `items: TranscriptItem[]` and render by discriminator:

```svelte
<script lang="ts">
	import AssistantBubble from './AssistantBubble.svelte';
	import MessageBubble from './MessageBubble.svelte';
	import ToolChip from './ToolChip.svelte';
	import type { RunStatus, TranscriptItem } from '$lib/types';

	let {
		items,
		streamingText = null,
		streamingStatus = null
	}: {
		items: TranscriptItem[];
		streamingText?: string | null;
		streamingStatus?: RunStatus | null;
	} = $props();

	let container: HTMLDivElement | undefined = $state();

	$effect(() => {
		// Snap to bottom whenever the list grows or streaming text updates.
		void items.length;
		void streamingText;
		void streamingStatus;
		if (container) container.scrollTop = container.scrollHeight;
	});

	let showStreamingBubble = $derived(streamingText !== null);
	// "Streaming" means deltas are still flowing — i.e. the run hasn't
	// reached a terminal status yet. Guards the O(n²) markdown render
	// during streaming (we show plain text instead) and the pulsing dot.
	let isActivelyStreaming = $derived(
		streamingStatus === null || streamingStatus === 'queued' || streamingStatus === 'running'
	);
</script>

<div bind:this={container} class="flex-1 overflow-y-auto px-6 py-4">
	<ul class="mx-auto flex max-w-3xl flex-col gap-4">
		{#each items as item (item.kind === 'message' ? item.id : item.call_id)}
			<li>
				{#if item.kind === 'message'}
					<MessageBubble message={item} />
				{:else}
					<ToolChip {item} />
				{/if}
			</li>
		{/each}
		{#if showStreamingBubble}
			<li>
				<AssistantBubble text={streamingText ?? ''} streaming={isActivelyStreaming} />
			</li>
		{/if}
	</ul>
</div>
```

(`MessageBubble` takes a `MessageOut`; `MessageItem` is a structural superset, so it passes as-is.)

- [ ] **Step 6: Implement — `ChatView.svelte` rework**

Replace the `messages` state and handlers with items-based equivalents. The changed parts:

```typescript
	let items = $state<TranscriptItem[]>(conversation.items);
```

In `attachRun`'s `subscribeRun` handlers, add tool handlers and adjust `onMessageComplete`:

```typescript
			onMessageComplete: (message) => {
				// Replay can deliver a message.complete the conversation GET
				// already included — dedupe by id rather than trust ordering.
				if (!items.some((i) => i.kind === 'message' && i.id === message.id)) {
					items = [...items, { ...message, kind: 'message' }];
				}
				streamingText = null;
			},
			onToolCall: (event) => {
				// Replay can re-deliver a call already present from the GET.
				if (!items.some((i) => i.kind === 'tool' && i.call_id === event.call_id)) {
					items = [
						...items,
						{
							kind: 'tool',
							run_id: event.run_id,
							call_id: event.call_id,
							tool: event.tool,
							arguments: event.arguments,
							result: null,
							is_error: null,
							created_at: new Date(Date.now()).toISOString()
						}
					];
				}
			},
			onToolResult: (event) => {
				items = items.map((i) =>
					i.kind === 'tool' && i.call_id === event.call_id
						? { ...i, result: event.result, is_error: event.is_error }
						: i
				);
			},
```

Every other `messages = ...` site becomes the `items` equivalent with `kind: 'message'` added to constructed objects: the optimistic user bubble in `handleSend`, its id-reconciliation `map` (guard with `i.kind === 'message'`), its rollback `filter`, and the terminal partial-bubble in `onEnded`. Update the type import list to include `TranscriptItem`. Pass `{items}` to `<MessageList {items} ... />`.

- [ ] **Step 7: Run all UI checks**

Run: `pnpm run test:unit -- --run && pnpm run check && pnpm run lint`
Expected: all PASS, 0 errors. If `src/routes/(app)/c/[id]/+page.ts` fails `check` on a `.messages` reference, update it to `items` here.

- [ ] **Step 8: Commit**

```bash
git add src/lib/types.ts src/lib/components src/routes
git commit -m "feat(ui): items-based transcript with live tool chips"
```

---

### Task 14: UI — MCP servers admin page

**Files:**
- Create: `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.ts`
- Create: `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte`
- Create: `rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts`
- Modify: `rehketo-ui/src/routes/(app)/settings/+page.svelte` (link, capability-gated)

- [ ] **Step 1: Write the failing test**

Create `src/routes/(app)/settings/mcp-servers/page.dom.spec.ts` (follow the structure of the existing `settings/page.dom.spec.ts` for mocking `$lib/api` — check that file's mock pattern and mirror it):

```typescript
import { mount, unmount } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import Page from './+page.svelte';
import { apiFetch } from '$lib/api';
import type { McpServerOut } from '$lib/types';

vi.mock('$lib/api', () => ({ apiFetch: vi.fn() }));
vi.mock('$lib/stores/toasts.svelte', () => ({ toasts: { push: vi.fn() } }));

function server(overrides: Partial<McpServerOut> = {}): McpServerOut {
	return {
		id: 's0000000-0000-0000-0000-000000000001',
		name: 'github',
		url: 'https://mcp.example.com/mcp',
		has_auth_token: true,
		allowed_roles: ['Admin', 'User'],
		enabled: true,
		created_at: '2026-06-11T00:00:00Z',
		updated_at: '2026-06-11T00:00:00Z',
		...overrides
	};
}

describe('MCP servers admin page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		document.body.innerHTML = '';
	});

	it('renders the loaded server list', () => {
		const app = mount(Page, {
			target: document.body,
			props: { data: { servers: [server()] } }
		});
		expect(document.body.textContent).toContain('github');
		expect(document.body.textContent).toContain('https://mcp.example.com/mcp');
		unmount(app);
	});

	it('creates a server and prepends it to the list', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ name: 'newsrv' }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { servers: [] } }
		});

		(document.querySelector('#mcp-name') as HTMLInputElement).value = 'newsrv';
		document.querySelector('#mcp-name')!.dispatchEvent(new Event('input'));
		(document.querySelector('#mcp-url') as HTMLInputElement).value =
			'https://new.example.com/mcp';
		document.querySelector('#mcp-url')!.dispatchEvent(new Event('input'));

		(document.querySelector('#mcp-create') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers',
				expect.objectContaining({ method: 'POST' })
			);
		});
		unmount(app);
	});

	it('toggles enabled via PATCH', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ enabled: false }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { servers: [server()] } }
		});
		(document.querySelector('[data-action="toggle"]') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers/s0000000-0000-0000-0000-000000000001',
				expect.objectContaining({ method: 'PATCH' })
			);
		});
		unmount(app);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm run test:unit -- --run src/routes`
Expected: FAIL — cannot resolve `./+page.svelte`

- [ ] **Step 3: Implement the load function**

Create `+page.ts`:

```typescript
import { apiFetch } from '$lib/api';
import type { McpServerList, McpServerOut } from '$lib/types';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => {
	const res = await apiFetch<McpServerList>('/admin/mcp-servers', { fetch });
	return { servers: res.items satisfies McpServerOut[] };
};
```

(Match the existing `settings/+page.ts` signature style — if `apiFetch` there doesn't take a `fetch` option, mirror exactly what that file does instead.)

- [ ] **Step 4: Implement the page**

Create `+page.svelte` — deliberately simple: list with enable/disable + delete, and a create form (name, URL, optional token, role checkboxes):

```svelte
<script lang="ts">
	import { apiFetch } from '$lib/api';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type McpServerOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const ROLES = ['Admin', 'Moderator', 'User'];

	// svelte-ignore state_referenced_locally
	let servers = $state<McpServerOut[]>(data.servers);

	let name = $state('');
	let url = $state('');
	let authToken = $state('');
	let allowedRoles = $state<string[]>([...ROLES]);
	let busy = $state(false);

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} MCP server.` });
		}
	}

	async function create(): Promise<void> {
		busy = true;
		try {
			const created = await apiFetch<McpServerOut>('/admin/mcp-servers', {
				method: 'POST',
				body: JSON.stringify({
					name,
					url,
					auth_token: authToken || null,
					allowed_roles: allowedRoles,
					enabled: true
				})
			});
			servers = [created, ...servers];
			name = '';
			url = '';
			authToken = '';
			toasts.push({ variant: 'info', message: 'MCP server added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			busy = false;
		}
	}

	async function toggle(server: McpServerOut): Promise<void> {
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ enabled: !server.enabled })
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}

	async function remove(server: McpServerOut): Promise<void> {
		if (!confirm(`Delete MCP server "${server.name}"?`)) return;
		try {
			await apiFetch(`/admin/mcp-servers/${server.id}`, { method: 'DELETE' });
			servers = servers.filter((s) => s.id !== server.id);
		} catch (err) {
			fail('delete', err);
		}
	}
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">MCP servers</h1>
	<p class="mt-1 text-sm text-muted">
		External tool servers available to agent runs. Granted roles get all of a
		server's tools; disable to take a server offline without deleting it.
	</p>

	<ul class="mt-6 flex flex-col gap-3">
		{#each servers as server (server.id)}
			<li class="rounded-md border border-border bg-surface p-3">
				<div class="flex items-center justify-between gap-3">
					<div>
						<span class="font-mono text-sm">{server.name}</span>
						{#if !server.enabled}
							<span class="ml-2 text-xs text-muted">disabled</span>
						{/if}
						<p class="text-xs text-muted">{server.url}</p>
						<p class="text-xs text-muted">
							roles: {server.allowed_roles.join(', ')}
							{#if server.has_auth_token}
								· token set{/if}
						</p>
					</div>
					<div class="flex gap-2">
						<button
							type="button"
							data-action="toggle"
							onclick={() => toggle(server)}
							class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
						>
							{server.enabled ? 'Disable' : 'Enable'}
						</button>
						<button
							type="button"
							data-action="delete"
							onclick={() => remove(server)}
							class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
						>
							Delete
						</button>
					</div>
				</div>
			</li>
		{:else}
			<li class="text-sm text-muted">No servers configured.</li>
		{/each}
	</ul>

	<section class="mt-8 rounded-md border border-border bg-surface p-4">
		<h2 class="text-sm font-semibold">Add server</h2>
		<div class="mt-3 flex flex-col gap-3">
			<label class="text-xs text-muted" for="mcp-name">Name (tool prefix)</label>
			<input
				id="mcp-name"
				bind:value={name}
				placeholder="github"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<label class="text-xs text-muted" for="mcp-url">URL</label>
			<input
				id="mcp-url"
				bind:value={url}
				placeholder="https://host/mcp"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<label class="text-xs text-muted" for="mcp-token">Bearer token (optional, write-only)</label>
			<input
				id="mcp-token"
				bind:value={authToken}
				type="password"
				autocomplete="off"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<fieldset class="flex gap-4 text-sm">
				<legend class="text-xs text-muted">Allowed roles</legend>
				{#each ROLES as role (role)}
					<label class="flex items-center gap-1">
						<input type="checkbox" value={role} bind:group={allowedRoles} />
						{role}
					</label>
				{/each}
			</fieldset>
			<button
				id="mcp-create"
				type="button"
				onclick={create}
				disabled={busy || !name || !url}
				class="self-end rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
			>
				Add
			</button>
		</div>
	</section>
</div>
```

- [ ] **Step 5: Link from settings (capability-gated)**

In `src/routes/(app)/settings/+page.svelte`, import the auth store (`import { auth } from '$lib/stores/auth.svelte';`) and add after the custom-instructions `</section>`:

```svelte
{#if auth.can('admin.manage_mcp_servers')}
	<section class="mt-8">
		<h2 class="text-sm font-semibold">Administration</h2>
		<a href="/settings/mcp-servers" class="mt-2 inline-block text-sm text-accent hover:underline">
			Manage MCP servers →
		</a>
	</section>
{/if}
```

(Capabilities come from `GET /me/capabilities` via the auth store — never reconstructed in the frontend.)

- [ ] **Step 6: Run all UI checks**

Run: `pnpm run test:unit -- --run && pnpm run check && pnpm run lint`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add src/routes/\(app\)/settings src/lib
git commit -m "feat(ui): MCP servers admin page"
```

---

### Task 15: Docs — roadmap M3.5 + per-tool follow-up + spec amendment

**Files:**
- Modify: `docs/superpowers/specs/2026-06-10-roadmap-family-launch-design.md`
- Modify: `docs/superpowers/specs/2026-06-11-mcp-tool-calling-design.md`

- [ ] **Step 1: Roadmap — add M3.5 between M3 and M4**

Insert after the M3 section:

```markdown
### M3.5 — Per-call tool approval

Run pauses on a tool call pending user approval in the chat UI, with a
per-server `auto_approve` flag so trusted servers keep M3's auto-execute
behavior. Pulls in LangGraph interrupt/resume, a pending-approval run state,
new SSE events, and approval UI — deliberately split out of M3 so plain tool
calling ships first.

Also queued from M3: per-tool allowlist granularity (M3 gates per server;
per-tool waits for a real case that demands it).
```

- [ ] **Step 2: Roadmap — M3 section pointer**

At the end of the M3 section add: `Spec: \`2026-06-11-mcp-tool-calling-design.md\`.`

- [ ] **Step 3: Spec amendment — publish serialization**

In `2026-06-11-mcp-tool-calling-design.md`, append to the **SSE events** section:

```markdown
Implementation note (discovered in planning): LangGraph executes parallel
tool calls concurrently, so a run can have several publishers racing the
bus's `MAX(sequence)+1` insert. `PostgresEventBus.publish` serializes
publishes with a process-local per-run `asyncio.Lock` (all of a run's
publishers share one process, today and after the M4 worker split).
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs
git commit -m "docs: M3.5 roadmap item and M3 spec amendments"
```

---

### Task 16: Full validation

- [ ] **Step 1: Repo guards** (from repo root)

```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```
Expected: both pass

- [ ] **Step 2: API checks** (from `rehketo-api/`)

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run python ../tools/check_contract.py
```
Expected: all pass — quote the real output (charter rule 5)

- [ ] **Step 3: UI checks** (from `rehketo-ui/`)

```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```
Expected: all pass — quote the real output

- [ ] **Step 4: Manual smoke (optional but recommended)**

`just db`, `just api`, `just ui` in three terminals; add a local MCP server (e.g. a trivial fastmcp HTTP server) on the admin page, send a chat message that triggers a tool, watch the chip stream and survive reload.

---

## Self-review notes (already applied)

- **Spec coverage:** schema → T3; admin API → T5; gate → T1; runtime package → T6–T8; per-run lifecycle + run wiring → T9; import contracts → T10; SSE events → T7 (publisher) + T12 (consumer); 16KB cap → T7; transcript reload → T11 (API) + T13 (UI); tool chips → T13; admin page → T14; error handling (skip/`is_error`) → T8/T7; roadmap edits → T15; validation → T16.
- **Discovered gap fixed as Task 2:** concurrent publish sequence race (documented in spec by T15).
- **Type consistency:** `allowed_servers(db, roles)`, `build_run_toolset(stack, servers, *, run_id, bus)`, `build_structured_tool(server_name=, tool=, client=, run_id=, bus=)`, `McpServer.auth_token_ct`, `ToolCallItem`/`MessageItem`/`TranscriptItem`, `items` — used identically across tasks.
- **Known judgment calls for the executor:** exact `format_exc_for_log` import location (T7 note); pre-existing tests asserting `messages` (T11 step 4); `+page.ts` load-signature mirroring (T14 step 3).
