# User Preferences (M2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-user custom instructions stored in a new `user_preferences` table, edited on a `/settings` page, and injected into the agent's system prompt through a new prompt-assembly seam.

**Architecture:** A 1:1 `user_preferences` table (user-owned write path, separate from the auth-owned `users` table) is read once at run start by `run.py`, assembled into the system prompt by a new pure function in `rehketo/agent/prompt.py`, and passed into `build_agent` (which loses its hardcoded prompt). The API exposes `GET/PUT /me/preferences`; the UI adds a `/settings` route reached from the UserMenu.

**Tech Stack:** FastAPI + async SQLAlchemy/psycopg3 + Alembic (rehketo-api), SvelteKit + Svelte 5 runes + Vitest (rehketo-ui).

**Spec:** `docs/superpowers/specs/2026-06-10-user-preferences-design.md`

**Conventions that bind every task:** Conventional Commits, NO AI-attribution trailers (enforced by pre-commit), every Python function fully annotated, `Annotated[T, Depends(...)]` for FastAPI deps, tests assert behavior not implementation. API commands run from `rehketo-api/`, UI commands from `rehketo-ui/`.

---

### Task 1: Prompt-assembly seam (`rehketo/agent/prompt.py`)

Pure function, no dependencies — TDD it first.

**Files:**
- Create: `rehketo-api/rehketo/agent/prompt.py`
- Test: `rehketo-api/tests/unit/test_prompt_assembly.py`

- [ ] **Step 1: Write the failing test**

Create `rehketo-api/tests/unit/test_prompt_assembly.py`:

```python
from __future__ import annotations

from rehketo.agent.prompt import BASE_SYSTEM_PROMPT, assemble_system_prompt


def test_none_returns_base_prompt() -> None:
    assert assemble_system_prompt(None) == BASE_SYSTEM_PROMPT


def test_blank_returns_base_prompt() -> None:
    assert assemble_system_prompt("   \n") == BASE_SYSTEM_PROMPT


def test_instructions_appended_under_delimited_section() -> None:
    result = assemble_system_prompt("Always answer in haiku.")
    assert result.startswith(BASE_SYSTEM_PROMPT)
    assert "## User instructions" in result
    assert result.endswith("Always answer in haiku.")


def test_instructions_are_stripped() -> None:
    result = assemble_system_prompt("  Be terse.  \n")
    assert result.endswith("Be terse.")
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `rehketo-api/`): `uv run pytest tests/unit/test_prompt_assembly.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.agent.prompt'`

- [ ] **Step 3: Write the implementation**

Create `rehketo-api/rehketo/agent/prompt.py`:

```python
"""System prompt assembly — the single seam where per-user context joins the
base prompt. Compaction (see the roadmap's event-gated items) will plug in
here too; until then this stays a pure function with no I/O."""

from __future__ import annotations

BASE_SYSTEM_PROMPT = "You are a helpful assistant."


def assemble_system_prompt(custom_instructions: str | None) -> str:
    if custom_instructions is None or not custom_instructions.strip():
        return BASE_SYSTEM_PROMPT
    return (
        f"{BASE_SYSTEM_PROMPT}\n\n## User instructions\n{custom_instructions.strip()}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_prompt_assembly.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/agent/prompt.py rehketo-api/tests/unit/test_prompt_assembly.py
git commit -m "feat: add system prompt assembly seam"
```

---

### Task 2: `UserPreferences` model + migration 0010

**Files:**
- Modify: `rehketo-api/rehketo/db/models.py` (after `UserRole`, ~line 119)
- Create: `rehketo-api/alembic/versions/0010_user_preferences.py`

Convention notes: cascade behavior lives in migrations only (`ondelete="CASCADE"` in the migration, plain `ForeignKey` in the model — see `0006_cascade_deletes.py`); revision ids are zero-padded numeric.

- [ ] **Step 1: Add the ORM model**

In `rehketo-api/rehketo/db/models.py`, insert after the `UserRole` class (which ends at line 118):

```python
class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    custom_instructions: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

All imports used (`Mapped`, `mapped_column`, `PGUUID`, `ForeignKey`, `Text`, `DateTime`, `func`, `datetime`) already exist at the top of the file.

- [ ] **Step 2: Create the migration**

Create `rehketo-api/alembic/versions/0010_user_preferences.py`:

```python
"""per-user preferences: custom instructions

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-10 00:00:00.000000+00:00

1:1 with users; row created on first save by the API, never by the auth
flow. No row means "no preferences set".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("custom_instructions", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
```

- [ ] **Step 3: Verify the migration round-trips**

Requires the local dev postgres (`just db` from the repo root, in its own terminal). From `rehketo-api/`:

```bash
uv run alembic upgrade head
uv run alembic downgrade 0009
uv run alembic upgrade head
```

Expected: each command logs `INFO  [alembic.runtime.migration]` lines; the last ends with `Running upgrade 0009 -> 0010`. No tracebacks.

- [ ] **Step 4: Run the model-compile unit test and lint**

```bash
uv run pytest tests/unit/test_models_compile.py -v
uv run ruff check && uv run mypy rehketo
```

Expected: pytest passes; ruff and mypy report no errors.

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/db/models.py rehketo-api/alembic/versions/0010_user_preferences.py
git commit -m "feat: add user_preferences table and model"
```

---

### Task 3: `GET/PUT /me/preferences`

**Files:**
- Modify: `rehketo-api/rehketo/api/me.py`
- Test: `rehketo-api/tests/integration/test_me_preferences.py`
- Modify (rebaseline): `rehketo-ui/openapi.snapshot.json`

- [ ] **Step 1: Write the failing tests**

Create `rehketo-api/tests/integration/test_me_preferences.py` (fixtures `settings_env`/`db_url`/`db` come from `tests/conftest.py`; integration tests hit real postgres via testcontainers — never mock the DB):

```python
from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import User, UserRole
from rehketo.main import create_app


async def _seed_session(db) -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email="al@example.com")
    db.add_all([u, UserRole(user_id=u.id, role="User")])
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(sid), issue_csrf_token(str(sid))


async def test_get_preferences_empty_without_row(settings_env, db_url, db) -> None:
    sid, _ = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
    assert r.status_code == 200
    assert r.json() == {"custom_instructions": ""}


async def test_put_creates_then_updates(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        cookies = {SESSION_COOKIE: sid, CSRF_COOKIE: csrf}
        headers = {CSRF_HEADER: csrf}

        r = await c.put(
            "/me/preferences",
            cookies=cookies,
            headers=headers,
            json={"custom_instructions": "Answer in haiku."},
        )
        assert r.status_code == 200
        assert r.json() == {"custom_instructions": "Answer in haiku."}

        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
        assert r.json() == {"custom_instructions": "Answer in haiku."}

        r = await c.put(
            "/me/preferences",
            cookies=cookies,
            headers=headers,
            json={"custom_instructions": "Be terse."},
        )
        assert r.status_code == 200

        r = await c.get("/me/preferences", cookies={SESSION_COOKIE: sid})
        assert r.json() == {"custom_instructions": "Be terse."}


async def test_put_over_limit_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put(
            "/me/preferences",
            cookies={SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"custom_instructions": "x" * 4001},
        )
    assert r.status_code == 422


async def test_get_preferences_unauthenticated_is_401(settings_env, db_url, db) -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/preferences")
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_me_preferences.py -v`
Expected: 4 FAILED — each with `assert 404 == ...` (route does not exist yet; 401 test fails because unauthenticated 404 ≠ 401 — FastAPI matches no route).

- [ ] **Step 3: Implement the endpoints**

In `rehketo-api/rehketo/api/me.py`:

Replace the import block lines

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
```

with

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
```

and change

```python
from rehketo.db.models import User
```

to

```python
from rehketo.db.models import User, UserPreferences
```

Append at the end of the file:

```python
class PreferencesOut(BaseModel):
    custom_instructions: str


class PreferencesIn(BaseModel):
    custom_instructions: str = Field(max_length=4000)


@router.get("/me/preferences", response_model=PreferencesOut)
async def get_preferences(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> PreferencesOut:
    prefs = (
        await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    return PreferencesOut(
        custom_instructions=prefs.custom_instructions if prefs else ""
    )


@router.put("/me/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> PreferencesOut:
    stmt = (
        pg_insert(UserPreferences)
        .values(user_id=perms.user_id, custom_instructions=body.custom_instructions)
        .on_conflict_do_update(
            index_elements=[UserPreferences.user_id],
            set_={
                "custom_instructions": body.custom_instructions,
                "updated_at": func.now(),
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
    return PreferencesOut(custom_instructions=body.custom_instructions)
```

Notes: the target row is always the session user's (`perms.user_id`), so no resource-level `permissions.require` call is needed — this matches the existing `/me` handler. CSRF on PUT is enforced by the existing `CSRFMiddleware`; no code needed here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_me_preferences.py tests/integration/test_me.py -v`
Expected: all pass (including the pre-existing `/me` test).

- [ ] **Step 5: Rebaseline the OpenAPI snapshot**

```bash
uv run python ../tools/check_contract.py --update
uv run python ../tools/check_contract.py
```

Expected: the second run exits 0 (`git diff rehketo-ui/openapi.snapshot.json` shows only the two new `/me/preferences` operations and schemas).

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format --check && uv run ruff check && uv run mypy rehketo
git add rehketo-api/rehketo/api/me.py rehketo-api/tests/integration/test_me_preferences.py rehketo-ui/openapi.snapshot.json
git commit -m "feat: add GET/PUT /me/preferences endpoints"
```

---

### Task 4: Thread the assembled prompt through `run.py` → `build_agent`

`build_agent` gains a required positional `system_prompt` parameter; `run.py` fetches preferences at run start and assembles the prompt. Every test that fakes `build_agent` must gain the second parameter — the full list is below.

**Files:**
- Modify: `rehketo-api/rehketo/agent/graph.py`
- Modify: `rehketo-api/rehketo/agent/run.py`
- Modify: `rehketo-api/tests/integration/_helpers.py:155`
- Modify: `rehketo-api/tests/integration/test_run_agent_end_to_end.py:60`
- Modify: `rehketo-api/tests/integration/test_sse_resume_by_sequence.py:52`
- Modify: `rehketo-api/tests/integration/test_run_cancel.py:47`
- Modify: `rehketo-api/tests/integration/test_sse_csrf_exempt.py:40`
- Modify: `rehketo-api/tests/integration/test_run_cancel_shield.py:39`
- Modify: `rehketo-api/tests/integration/test_e2e_chat_smoke.py:31`
- Modify: `rehketo-api/tests/integration/test_run_agent_conversation_updated.py:43`
- Modify: `rehketo-api/tests/integration/test_run_outcome_persistence.py:41,54,217`
- Modify: `rehketo-api/tests/integration/test_agent_canary.py:37`
- Test: `rehketo-api/tests/integration/test_run_uses_preferences.py`

- [ ] **Step 1: Write the failing integration test**

Create `rehketo-api/tests/integration/test_run_uses_preferences.py`:

```python
"""Run orchestration must read the user's stored custom instructions at run
start, assemble them into the system prompt, and pass that to build_agent.

Patches ``rehketo.agent.run.build_agent`` (the binding run_agent actually
calls — see test_run_agent_end_to_end.py for why) with a fake that captures
the prompt it was given.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from rehketo.agent.prompt import BASE_SYSTEM_PROMPT
from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserPreferences, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

captured: dict[str, str] = {}


class _OkAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        await asyncio.sleep(0)
        yield (AIMessageChunk(content="ok", id="m1"), {"langgraph_node": "agent"})


async def _fake_build_agent(
    run_id: str, system_prompt: str
) -> AsyncIterator[_OkAgent]:
    captured["system_prompt"] = system_prompt
    yield _OkAgent()


async def _post_and_drain(c: AsyncClient, conv_id: str, sid: str, csrf: str) -> None:
    r = await c.post(
        f"/conversations/{conv_id}/messages",
        cookies={SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
        json={"content": "hi"},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    async with c.stream(
        "GET", f"/runs/{run_id}/events", cookies={SESSION_COOKIE: sid}
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                if json.loads(line[6:])["type"] == "run.ended":
                    break


async def _seed(db, *, instructions: str | None) -> tuple[str, str, str]:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    rows: list[object] = [UserRole(user_id=u.id, role="User"), conv]
    if instructions is not None:
        rows.append(UserPreferences(user_id=u.id, custom_instructions=instructions))
    db.add_all(rows)
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(conv.id), str(sid), issue_csrf_token(str(sid))


async def test_run_passes_assembled_prompt(
    settings_env, db_url, db, monkeypatch
) -> None:
    reset_registry_for_tests()
    captured.clear()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    conv_id, sid, csrf = await _seed(db, instructions="Answer in haiku.")

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _post_and_drain(c, conv_id, sid, csrf)

    prompt = captured["system_prompt"]
    assert prompt.startswith(BASE_SYSTEM_PROMPT)
    assert "## User instructions" in prompt
    assert "Answer in haiku." in prompt


async def test_run_without_preferences_uses_base_prompt(
    settings_env, db_url, db, monkeypatch
) -> None:
    reset_registry_for_tests()
    captured.clear()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    conv_id, sid, csrf = await _seed(db, instructions=None)

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _post_and_drain(c, conv_id, sid, csrf)

    assert captured["system_prompt"] == BASE_SYSTEM_PROMPT
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run pytest tests/integration/test_run_uses_preferences.py -v`
Expected: FAIL — `KeyError: 'system_prompt'`. (run.py still calls `build_agent(str(run_id))` with one argument; the two-argument fake raises `TypeError` inside the run task, the run finishes as `failed`, `run.ended` still arrives, and the test's `captured["system_prompt"]` lookup then fails.)

- [ ] **Step 3: Change `build_agent` to accept the prompt**

In `rehketo-api/rehketo/agent/graph.py`, replace:

```python
async def build_agent(run_id: str) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. Tools list is empty for v1 — infrastructure
    only. The graph is a LangGraph CompiledStateGraph; deepagents accepts
    `checkpointer=` as a constructor kwarg (verified against the real API).
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=[],
            system_prompt="You are a helpful assistant.",
            model=build_chat_model(),
            checkpointer=saver,
        )
        yield agent
```

with:

```python
async def build_agent(
    run_id: str, system_prompt: str
) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. Tools list is empty for v1 — infrastructure
    only. The system prompt is assembled by the caller (rehketo.agent.prompt)
    so graph construction stays a pure function of its inputs. The graph is a
    LangGraph CompiledStateGraph; deepagents accepts `checkpointer=` as a
    constructor kwarg (verified against the real API).
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=[],
            system_prompt=system_prompt,
            model=build_chat_model(),
            checkpointer=saver,
        )
        yield agent
```

- [ ] **Step 4: Fetch preferences and assemble in `run.py`**

In `rehketo-api/rehketo/agent/run.py`:

Add the import (after `from rehketo.agent.graph import build_agent`):

```python
from rehketo.agent.prompt import assemble_system_prompt
```

Extend the models import:

```python
from rehketo.db.models import Conversation, Message, Run, UserPreferences
```

In the opening DB block of `run_agent`, after `history = await _load_history(db, conversation_id)` (line 83), add:

```python
        prefs = (
            await db.execute(
                select(UserPreferences).where(UserPreferences.user_id == run.user_id)
            )
        ).scalar_one_or_none()
        system_prompt = assemble_system_prompt(
            prefs.custom_instructions if prefs else None
        )
```

Change the call site (line 89) from:

```python
            async for agent in build_agent(str(run_id)):
```

to:

```python
            async for agent in build_agent(str(run_id), system_prompt):
```

- [ ] **Step 5: Update every test fake of `build_agent`**

Each fake gains the second positional parameter. Exact line edits (line numbers as of the current tree):

`tests/integration/_helpers.py:155`
```python
    async def _build(_run_id: str, _system_prompt: str) -> AsyncIterator[FakeStreamingAgent]:
```

`tests/integration/test_run_agent_end_to_end.py:60`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_HelloAgent]:
```

`tests/integration/test_sse_resume_by_sequence.py:52`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_SlowAgent]:
```

`tests/integration/test_run_cancel.py:47`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_NeverStreamingAgent]:
```

`tests/integration/test_sse_csrf_exempt.py:40`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_ImmediateAgent]:
```

`tests/integration/test_run_cancel_shield.py:39`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_NeverStreamingAgent]:
```

`tests/integration/test_e2e_chat_smoke.py:31`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> Any:
```

`tests/integration/test_run_agent_conversation_updated.py:43`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_HiAgent]:
```

`tests/integration/test_run_outcome_persistence.py:41`
```python
async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_PartialThenHangAgent]:
```

`tests/integration/test_run_outcome_persistence.py:54`
```python
async def _fake_failing_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_RaisingAgent]:
```

`tests/integration/test_run_outcome_persistence.py:217` (inline `_build` inside the test body)
```python
    async def _build(run_id: str, system_prompt: str) -> AsyncIterator[_HiAgent]:
```

`tests/integration/test_agent_canary.py` — this one calls the REAL `build_agent`. Add the import:

```python
from rehketo.agent.prompt import BASE_SYSTEM_PROMPT
```

and change line 37 from `async for graph in build_agent("canary-run"):` to:

```python
    async for graph in build_agent("canary-run", BASE_SYSTEM_PROMPT):
```

(If any line number has drifted, locate by the function signature text — each file has exactly one definition.)

- [ ] **Step 6: Run the new test and the full API suite**

```bash
uv run pytest tests/integration/test_run_uses_preferences.py -v
uv run pytest
```

Expected: new tests pass; full suite passes (no fake left on the old one-argument signature).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff format --check && uv run ruff check && uv run mypy rehketo
git add rehketo-api/rehketo/agent/graph.py rehketo-api/rehketo/agent/run.py rehketo-api/tests
git commit -m "feat: inject user custom instructions into the agent system prompt"
```

---

### Task 5: UI — `/settings` page

**Files:**
- Modify: `rehketo-ui/src/lib/types.ts`
- Create: `rehketo-ui/src/routes/(app)/settings/+page.ts`
- Create: `rehketo-ui/src/routes/(app)/settings/+page.svelte`
- Modify: `rehketo-ui/src/lib/components/UserMenu.svelte`
- Test: `rehketo-ui/src/routes/(app)/settings/page.dom.spec.ts`

UI rules that bind here: Svelte 5 runes only; `apiFetch` is the only fetch wrapper; dark workbench theme tokens (`bg-surface`, `border-border`, `text-muted`, `bg-accent`, `text-danger`).

- [ ] **Step 1: Add the contract type**

In `rehketo-ui/src/lib/types.ts`, after the `CapabilitiesOut` type, add:

```typescript
// Matches rehketo-api/rehketo/api/me.py PreferencesOut.
export type PreferencesOut = {
	custom_instructions: string;
};
```

- [ ] **Step 2: Write the failing dom spec**

Create `rehketo-ui/src/routes/(app)/settings/page.dom.spec.ts`:

```typescript
// Settings page: renders the loaded instructions, gates Save on dirty/limit,
// and PUTs through apiFetch. $lib/api is mocked — no network.

import { flushSync, mount, unmount } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsPage from './+page.svelte';
import { apiFetch } from '$lib/api';

vi.mock('$lib/api', () => ({
	apiFetch: vi.fn(async () => ({ custom_instructions: 'updated' }))
}));

function mountPage(instructions: string) {
	return mount(SettingsPage, {
		target: document.body,
		props: { data: { preferences: { custom_instructions: instructions } } }
	});
}

function setTextarea(value: string): HTMLTextAreaElement {
	const textarea = document.querySelector('textarea');
	if (!textarea) throw new Error('textarea not rendered');
	textarea.value = value;
	textarea.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	return textarea;
}

describe('settings page', () => {
	beforeEach(() => {
		document.body.innerHTML = '';
		vi.clearAllMocks();
	});

	it('renders the loaded instructions in the textarea', () => {
		const app = mountPage('be terse');
		const textarea = document.querySelector('textarea');
		expect(textarea?.value).toBe('be terse');
		unmount(app);
	});

	it('disables Save until the value changes', () => {
		const app = mountPage('be terse');
		const button = document.querySelector('button');
		expect(button?.disabled).toBe(true);
		setTextarea('be verbose');
		expect(button?.disabled).toBe(false);
		unmount(app);
	});

	it('disables Save when over the 4000-character limit', () => {
		const app = mountPage('');
		setTextarea('x'.repeat(4001));
		const button = document.querySelector('button');
		expect(button?.disabled).toBe(true);
		unmount(app);
	});

	it('PUTs the new value through apiFetch on Save', async () => {
		const app = mountPage('be terse');
		setTextarea('be verbose');
		document.querySelector('button')?.click();
		flushSync();
		expect(apiFetch).toHaveBeenCalledWith('/me/preferences', {
			method: 'PUT',
			body: JSON.stringify({ custom_instructions: 'be verbose' })
		});
		unmount(app);
	});
});
```

- [ ] **Step 3: Run the spec to verify it fails**

Run (from `rehketo-ui/`): `pnpm run test:unit -- --run src/routes`
Expected: FAIL — cannot resolve `./+page.svelte` (file does not exist yet).

- [ ] **Step 4: Create the load function**

Create `rehketo-ui/src/routes/(app)/settings/+page.ts` (the 401-redirect shape copies `(app)/c/[id]/+page.ts` — see the comment there for why `skipAuthRedirect` is set):

```typescript
import { redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { ApiError, type PreferencesOut } from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const preferences = await apiFetch<PreferencesOut>('/me/preferences', {
			skipAuthRedirect: true
		});
		return { preferences };
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			const next = encodeURIComponent(url.pathname + url.search);
			throw redirect(302, `/login?next=${next}`);
		}
		throw err;
	}
};
```

- [ ] **Step 5: Create the page component**

Create `rehketo-ui/src/routes/(app)/settings/+page.svelte`:

```svelte
<script lang="ts">
	import { apiFetch } from '$lib/api';
	import { toasts } from '$lib/stores/toasts.svelte';
	import type { PreferencesOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const MAX_LENGTH = 4000;

	let value = $state(data.preferences.custom_instructions);
	let saved = $state(data.preferences.custom_instructions);
	let saving = $state(false);

	let overLimit = $derived(value.length > MAX_LENGTH);
	let dirty = $derived(value !== saved);

	async function save(): Promise<void> {
		saving = true;
		try {
			const res = await apiFetch<PreferencesOut>('/me/preferences', {
				method: 'PUT',
				body: JSON.stringify({ custom_instructions: value })
			});
			saved = res.custom_instructions;
			toasts.push({ variant: 'info', message: 'Preferences saved.' });
		} catch {
			toasts.push({ variant: 'error', message: 'Could not save preferences.' });
		} finally {
			saving = false;
		}
	}
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">Settings</h1>

	<section class="mt-6">
		<h2 class="text-sm font-semibold">Custom instructions</h2>
		<p class="mt-1 text-sm text-muted">Included in every new chat.</p>
		<textarea
			bind:value
			rows="8"
			placeholder="How should the assistant behave?"
			class="mt-3 w-full resize-y rounded-md border border-border bg-surface p-3 text-sm"
		></textarea>
		<div class="mt-2 flex items-center justify-between">
			<span class="text-xs {overLimit ? 'text-danger' : 'text-muted'}">
				{value.length} / {MAX_LENGTH}
			</span>
			<button
				type="button"
				onclick={save}
				disabled={!dirty || overLimit || saving}
				class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
			>
				Save
			</button>
		</div>
	</section>
</div>
```

- [ ] **Step 6: Run the spec to verify it passes**

Run: `pnpm run test:unit -- --run src/routes`
Expected: 4 passed.

- [ ] **Step 7: Add the UserMenu entry point**

In `rehketo-ui/src/lib/components/UserMenu.svelte`, inside the dropdown `<div>` (the one opened by `{#if open}`), insert a Settings button ABOVE the existing "Log out" button:

```svelte
				<button
					type="button"
					onclick={() => {
						open = false;
						void goto('/settings');
					}}
					class="block w-full px-3 py-2 text-left hover:bg-surface-hover"
				>
					Settings
				</button>
```

(`goto` is already imported at the top of the file.)

- [ ] **Step 8: Full UI validation**

```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

Expected: all three pass with no errors.

- [ ] **Step 9: Commit**

```bash
git add rehketo-ui/src/lib/types.ts "rehketo-ui/src/routes/(app)/settings" rehketo-ui/src/lib/components/UserMenu.svelte
git commit -m "feat: add settings page for user custom instructions"
```

---

### Task 6: Full validation sweep

Run every check from AGENTS.md and quote real output (charter rule 5). Nothing should need fixing if Tasks 1–5 validated as they went — this is the final gate.

- [ ] **Step 1: Repo-wide guards (from the repo root)**

```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

Expected: both exit 0.

- [ ] **Step 2: API checks (from `rehketo-api/`)**

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run python ../tools/check_contract.py
```

Expected: all pass; pytest reports 0 failures.

- [ ] **Step 3: UI checks (from `rehketo-ui/`)**

```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

Expected: all pass.

- [ ] **Step 4: Manual smoke (optional but recommended)**

In three terminals from the repo root: `just db`, `just api`, `just ui`. Sign in, open Settings from the user menu, save instructions ("Always answer in haiku."), start a NEW chat, and confirm the reply follows them. Edit + save again and confirm the next message reflects the change.
