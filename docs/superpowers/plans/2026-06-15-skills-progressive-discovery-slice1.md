# M4.5 Skills — Slice 1 (Spike) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a DB-backed skill registry into deepagents' native `SkillsMiddleware` (doc-skills) and `subagents=` (MCP-skills) so the agent discovers and uses capabilities by description, and measure whether it improves tool selection.

**Architecture:** A new `skills` table is the source of truth. At run start, `resolve_skills()` returns the user's role-allowed global skills ∪ owned skills (MCP-skills cross-checked against `allowed_servers()`). Doc-skills are materialized as in-state `SKILL.md` files for `SkillsMiddleware`; MCP-skills become `SubAgent` specs (description = trigger, tools = that server's adapted tools, `interrupt_on` = M3.5 config). No hand-rolled activation tool — deepagents provides both mechanisms.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2 (async), Alembic, deepagents/LangGraph, fastmcp, pytest. Validation commands from `rehketo-api/AGENTS.md`.

**Scope:** Slice 1 only — model + migration, resolution, wiring, tests, and an eval harness. Seed skills directly in the DB; no admin/UI (slices 2–3). All paths are relative to `rehketo-api/` unless absolute.

---

### Task 1: `Skill` model + migration `0014`

**Files:**
- Modify: `rehketo/db/models.py` (append after `McpServer`, ~line 155)
- Create: `alembic/versions/0014_skills.py`
- Test: `tests/integration/test_skills_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_skills_model.py
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from rehketo.db.models import McpServer, Skill


async def test_doc_skill_persists(settings_env, db_url, db) -> None:
    db.add(
        Skill(
            id=uuid4(),
            name="expense-policy",
            display_name="Expense policy",
            trigger="use when answering questions about reimbursement",
            kind="doc",
            instructions="# Expense policy\nReimburse within 30 days.",
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()


async def test_mcp_skill_persists(settings_env, db_url, db) -> None:
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://github.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(srv)
    await db.commit()
    db.add(
        Skill(
            id=uuid4(),
            name="github",
            trigger="use when working with GitHub repos, PRs, or issues",
            kind="mcp",
            mcp_server_id=srv.id,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()


async def test_doc_skill_without_instructions_violates_check(
    settings_env, db_url, db
) -> None:
    db.add(
        Skill(
            id=uuid4(),
            name="bad-doc",
            trigger="x",
            kind="doc",
            instructions=None,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_skills_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'Skill'`.

- [ ] **Step 3: Add the `Skill` model**

Append to `rehketo/db/models.py` after the `McpServer` class:

```python
class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    # The "use when…" line — the discovery surface. For mcp-skills it becomes
    # the SubAgent description; for doc-skills the SKILL.md frontmatter desc.
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_server_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("mcp_servers.id")
    )
    instructions: Mapped[str | None] = mapped_column(Text)
    # NULL = global skill; else scoped to that user. Enforcement of user-scope
    # authoring is a later slice; the column + resolution rule exist now.
    owner_user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    allowed_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("kind in ('mcp','doc')", name="skills_kind_enum"),
        CheckConstraint(
            "(kind = 'mcp' AND mcp_server_id IS NOT NULL AND instructions IS NULL) "
            "OR (kind = 'doc' AND instructions IS NOT NULL AND mcp_server_id IS NULL)",
            name="skills_kind_backing",
        ),
    )
```

- [ ] **Step 4: Create migration `0014`**

```python
# alembic/versions/0014_skills.py
"""skills registry (M4.5 discovery)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-15 00:00:00.000000+00:00

A skill is a discovery card backed by either an MCP server (kind='mcp', one
skill per server in v1) or an authored markdown doc (kind='doc'). owner_user_id
NULL means global; the column exists now so user-scoped skills (a later slice)
need no schema change. The kind/backing check keeps the two shapes honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("mcp_server_id", sa.UUID(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.CheckConstraint("kind in ('mcp','doc')", name="skills_kind_enum"),
        sa.CheckConstraint(
            "(kind = 'mcp' AND mcp_server_id IS NOT NULL AND instructions IS NULL) "
            "OR (kind = 'doc' AND instructions IS NOT NULL AND mcp_server_id IS NULL)",
            name="skills_kind_backing",
        ),
    )
    op.create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_skills_owner_user_id", table_name="skills")
    op.drop_table("skills")
```

- [ ] **Step 5: Apply the migration and run tests**

Run:
```bash
just db            # if postgres is not already up
cd rehketo-api && uv run alembic upgrade head
uv run pytest tests/integration/test_skills_model.py -v
```
Expected: migration applies cleanly; all three tests PASS (the third raises `IntegrityError` from `skills_kind_backing`).

- [ ] **Step 6: Commit**

```bash
git add rehketo/db/models.py alembic/versions/0014_skills.py tests/integration/test_skills_model.py
git commit -m "feat: add skills registry table (M4.5)"
```

---

### Task 2: `resolve_skills()` — scope ∪ role resolution

**Files:**
- Create: `rehketo/mcp/skills.py`
- Test: `tests/integration/test_resolve_skills.py`

**Interface this task defines (used by later tasks):**
```python
@dataclass(frozen=True)
class ResolvedSkills:
    doc: list[Skill]   # kind='doc'
    mcp: list[Skill]   # kind='mcp', backing server is enabled + allowed

async def resolve_skills(
    db: AsyncSession, *, user_id: UUID, roles: Iterable[str]
) -> ResolvedSkills: ...
```

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_resolve_skills.py
from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import McpServer, Skill
from rehketo.mcp.skills import resolve_skills


async def test_global_role_and_owned_union(settings_env, db_url, db) -> None:
    me, other = uuid4(), uuid4()
    srv = McpServer(
        id=uuid4(), name="github", url="https://x/mcp", auth_token_ct=None,
        allowed_roles=["User"], enabled=True,
    )
    db.add(srv)
    db.add_all(
        [
            Skill(id=uuid4(), name="github", trigger="repos", kind="mcp",
                  mcp_server_id=srv.id, allowed_roles=["User"], enabled=True),
            Skill(id=uuid4(), name="policy", trigger="reimburse", kind="doc",
                  instructions="body", allowed_roles=["User"], enabled=True),
            Skill(id=uuid4(), name="admin-only", trigger="x", kind="doc",
                  instructions="body", allowed_roles=["Admin"], enabled=True),
            Skill(id=uuid4(), name="mine", trigger="x", kind="doc",
                  instructions="body", owner_user_id=me, allowed_roles=[],
                  enabled=True),
            Skill(id=uuid4(), name="theirs", trigger="x", kind="doc",
                  instructions="body", owner_user_id=other, allowed_roles=[],
                  enabled=True),
            Skill(id=uuid4(), name="off", trigger="x", kind="doc",
                  instructions="body", allowed_roles=["User"], enabled=False),
        ]
    )
    await db.commit()

    resolved = await resolve_skills(db, user_id=me, roles=["User"])
    doc_names = sorted(s.name for s in resolved.doc)
    mcp_names = sorted(s.name for s in resolved.mcp)
    # global User-role docs + my owned doc; NOT admin-only, theirs, or disabled
    assert doc_names == ["mine", "policy"]
    assert mcp_names == ["github"]


async def test_mcp_skill_dropped_when_server_not_allowed(
    settings_env, db_url, db
) -> None:
    srv = McpServer(
        id=uuid4(), name="github", url="https://x/mcp", auth_token_ct=None,
        allowed_roles=["Admin"], enabled=True,  # user lacks Admin
    )
    db.add(srv)
    db.add(
        Skill(id=uuid4(), name="github", trigger="repos", kind="mcp",
              mcp_server_id=srv.id, allowed_roles=["User"], enabled=True)
    )
    await db.commit()
    resolved = await resolve_skills(db, user_id=uuid4(), roles=["User"])
    assert resolved.mcp == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_resolve_skills.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rehketo.mcp.skills'`.

- [ ] **Step 3: Implement `resolve_skills`**

```python
# rehketo/mcp/skills.py
"""Resolve which skills a run may offer, and adapt them onto deepagents'
native primitives. A skill is global (owner_user_id NULL, role-gated) or
user-owned; mcp-skills are additionally cross-checked against allowed_servers
so we never offer a card for a server the user cannot run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from rehketo.db.models import Skill
from rehketo.mcp.servers import allowed_servers
from rehketo.permissions.resolved import ResolvedPermissions

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ResolvedSkills:
    doc: list[Skill]
    mcp: list[Skill]


async def resolve_skills(
    db: AsyncSession, *, user_id: UUID, roles: Iterable[str]
) -> ResolvedSkills:
    perms = ResolvedPermissions(user_id=user_id, roles=frozenset(roles))
    rows = (
        (
            await db.execute(
                select(Skill)
                .where(
                    Skill.enabled.is_(True),
                    or_(
                        Skill.owner_user_id.is_(None),
                        Skill.owner_user_id == user_id,
                    ),
                )
                .order_by(Skill.name)
            )
        )
        .scalars()
        .all()
    )
    # Global skills are role-gated like servers; owned skills bypass the role
    # gate (ownership is its own grant). The same permission the chat path uses.
    visible = [
        s
        for s in rows
        if s.owner_user_id == user_id
        or perms.can(
            "chat.use_mcp_server",
            resource_type="skill",
            resource_id=s.id,
            resource_roles=s.allowed_roles,
        )
    ]
    allowed_ids = {
        srv.id for srv in await allowed_servers(db, user_id=user_id, roles=roles)
    }
    return ResolvedSkills(
        doc=[s for s in visible if s.kind == "doc"],
        mcp=[s for s in visible if s.kind == "mcp" and s.mcp_server_id in allowed_ids],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_resolve_skills.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo/mcp/skills.py tests/integration/test_resolve_skills.py
git commit -m "feat: resolve skills by scope and role (M4.5)"
```

---

### Task 3: Materialize doc-skills into `SKILL.md` files

**Files:**
- Modify: `rehketo/mcp/skills.py`
- Test: `tests/unit/test_skill_materialize.py`

**Interface this task defines:**
```python
SKILLS_ROOT = "/skills/"
def doc_skill_files(skills: list[Skill]) -> dict[str, str]: ...
```
Returns a mapping of `"/skills/<name>/SKILL.md"` → file content, suitable to
pass as `agent.astream({"messages": [...], "files": doc_skill_files(...)})`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_skill_materialize.py
from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import Skill
from rehketo.mcp.skills import SKILLS_ROOT, doc_skill_files


def _doc(name: str, trigger: str, body: str) -> Skill:
    return Skill(
        id=uuid4(), name=name, trigger=trigger, kind="doc",
        instructions=body, allowed_roles=["User"], enabled=True,
    )


def test_emits_skill_md_per_skill() -> None:
    files = doc_skill_files([_doc("policy", "reimburse", "# Policy\nbody")])
    assert list(files) == [f"{SKILLS_ROOT}policy/SKILL.md"]
    content = files[f"{SKILLS_ROOT}policy/SKILL.md"]
    assert content.startswith("---\n")
    assert "name: policy" in content
    assert "description: reimburse" in content
    assert content.rstrip().endswith("body")


def test_empty_when_no_docs() -> None:
    assert doc_skill_files([]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_skill_materialize.py -v`
Expected: FAIL — `ImportError: cannot import name 'doc_skill_files'`.

- [ ] **Step 3: Implement materialization**

Add to `rehketo/mcp/skills.py`:

```python
SKILLS_ROOT = "/skills/"


def doc_skill_files(skills: list[Skill]) -> dict[str, str]:
    """Render each doc-skill as a SKILL.md (YAML frontmatter + body) keyed by
    the path SkillsMiddleware scans. deepagents reads these from agent state
    when the files are passed on invoke, so the DB stays the source of truth."""
    files: dict[str, str] = {}
    for s in skills:
        frontmatter = f"---\nname: {s.name}\ndescription: {s.trigger}\n---\n"
        files[f"{SKILLS_ROOT}{s.name}/SKILL.md"] = f"{frontmatter}\n{s.instructions}"
    return files
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_skill_materialize.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo/mcp/skills.py tests/unit/test_skill_materialize.py
git commit -m "feat: materialize doc-skills as SKILL.md for SkillsMiddleware (M4.5)"
```

---

### Task 4: Build `SubAgent` specs from mcp-skills

**Files:**
- Modify: `rehketo/mcp/skills.py`
- Test: `tests/integration/test_skill_subagents.py`

**Interface this task defines:**
```python
async def build_skill_subagents(
    stack: AsyncExitStack,
    mcp_skills: list[Skill],
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> list[dict[str, Any]]: ...
```
Reuses `build_run_toolset` (which opens clients on `stack`), then groups the
flat tool list by `"<server>__"` prefix into one `SubAgent` dict per mcp-skill.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_skill_subagents.py
from __future__ import annotations

import contextlib
from typing import Any
from uuid import uuid4

from fastmcp import Client, FastMCP

from rehketo.db.models import McpServer, Skill
from rehketo.mcp import registry
from rehketo.mcp.skills import build_skill_subagents
from rehketo.runs.event_bus import PostgresEventBus


async def test_one_subagent_per_mcp_skill(settings_env, monkeypatch) -> None:
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List PRs."""
        return "[]"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    srv = McpServer(
        id=uuid4(), name="github", url="https://x/mcp", auth_token_ct=None,
        allowed_roles=["User"], enabled=True, auto_approve=True,
    )
    skill = Skill(
        id=uuid4(), name="github", trigger="use when working with GitHub",
        kind="mcp", mcp_server_id=srv.id, allowed_roles=["User"], enabled=True,
    )

    bus = PostgresEventBus()
    async with contextlib.AsyncExitStack() as stack:
        subs = await build_skill_subagents(
            stack, [skill], [srv], run_id=str(uuid4()), bus=bus
        )

    assert len(subs) == 1
    sub: dict[str, Any] = subs[0]
    assert sub["name"] == "github"
    assert sub["description"] == "use when working with GitHub"
    assert [t.name for t in sub["tools"]] == ["github__list_prs"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_skill_subagents.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_skill_subagents'`.

- [ ] **Step 3: Implement subagent construction**

Add to `rehketo/mcp/skills.py` (extend the imports block as shown):

```python
# add to the top-of-file imports
from typing import Any

from rehketo.mcp.registry import build_run_toolset

# add under TYPE_CHECKING
    from collections.abc import Sequence
    from contextlib import AsyncExitStack

    from rehketo.db.models import McpServer
    from rehketo.runs.event_bus import RunEventBus


async def build_skill_subagents(
    stack: AsyncExitStack,
    mcp_skills: list[Skill],
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> list[dict[str, Any]]:
    """One SubAgent per mcp-skill, scoped to its server's tools. Reuses the
    existing toolset builder (clients live on `stack`); tools are grouped by
    the "<server>__" name prefix the adapter assigns, and each subagent carries
    its server's M3.5 interrupt_on subset so approval stays orthogonal."""
    by_id = {srv.id: srv for srv in servers}
    needed = [by_id[s.mcp_server_id] for s in mcp_skills if s.mcp_server_id in by_id]
    tools, interrupt_on = await build_run_toolset(
        stack, needed, run_id=run_id, bus=bus
    )
    subagents: list[dict[str, Any]] = []
    for skill in mcp_skills:
        server = by_id.get(skill.mcp_server_id)
        if server is None:
            continue
        prefix = f"{server.name}__"
        skill_tools = [t for t in tools if t.name.startswith(prefix)]
        if not skill_tools:
            # Server was unreachable at connect time; build_run_toolset skips it
            # (a broken tool server must not take the run down). Drop the skill.
            continue
        spec: dict[str, Any] = {
            "name": skill.name,
            "description": skill.trigger,
            "system_prompt": skill.instructions
            or f"You handle tasks where: {skill.trigger}.",
            "tools": skill_tools,
        }
        sub_interrupts = {k: v for k, v in interrupt_on.items() if k.startswith(prefix)}
        if sub_interrupts:
            spec["interrupt_on"] = sub_interrupts
        subagents.append(spec)
    return subagents
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_skill_subagents.py -v`
Expected: PASS — one subagent named `github` with tool `github__list_prs`.

- [ ] **Step 5: Commit**

```bash
git add rehketo/mcp/skills.py tests/integration/test_skill_subagents.py
git commit -m "feat: build SubAgent specs from mcp-skills (M4.5)"
```

---

### Task 5: Thread skills into `build_agent`

**Files:**
- Modify: `rehketo/agent/graph.py:25-48`
- Test: `tests/unit/test_build_agent_skills.py`

`build_agent` gains two optional params and forwards them to
`create_deep_agent`: `subagents` (the mcp-skill specs) and `skill_sources`
(the SkillsMiddleware source list, `[SKILLS_ROOT]` when doc-skills exist).
deepagents installs `SkillsMiddleware` when `skills=` is non-None and the
`task` tool when `subagents` is non-empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_build_agent_skills.py
from __future__ import annotations

from typing import Any

import rehketo.agent.graph as graph_mod


async def test_build_agent_forwards_skills_and_subagents(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_create_deep_agent(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "AGENT"

    class _NullSaver:
        async def __aenter__(self) -> _NullSaver:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(graph_mod, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(
        graph_mod.AsyncPostgresSaver, "from_conn_string",
        lambda dsn: _NullSaver(),
    )

    subs = [{"name": "github", "description": "repos", "tools": []}]
    async for agent in graph_mod.build_agent(
        "run-1", "sys", subagents=subs, skill_sources=["/skills/"]
    ):
        assert agent == "AGENT"
    assert captured["subagents"] == subs
    assert captured["skills"] == ["/skills/"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_build_agent_skills.py -v`
Expected: FAIL — `build_agent() got an unexpected keyword argument 'subagents'`.

- [ ] **Step 3: Extend `build_agent`**

Replace the body of `rehketo/agent/graph.py` from the `build_agent` signature down. New signature and call:

```python
async def build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[BaseTool] = (),
    interrupt_on: Mapping[str, InterruptOnConfig] | None = None,
    subagents: Sequence[Mapping[str, object]] | None = None,
    skill_sources: Sequence[str] | None = None,
) -> AsyncIterator[CompiledStateGraph]:  # type: ignore[type-arg]
    """Yield a deepagents graph bound to a postgres checkpointer.

    Scoped to thread_id=run_id. Tools and the per-tool approval config are
    assembled by the caller (rehketo.mcp.registry) so graph construction stays
    a pure function of its inputs. interrupt_on installs deepagents'
    HumanInTheLoopMiddleware. subagents (mcp-skills) and skill_sources
    (doc-skills, via SkillsMiddleware) are the M4.5 discovery surface — see
    rehketo.mcp.skills; deepagents adds the `task` delegation tool when
    subagents are present and SkillsMiddleware when skills= is set.
    """
    dsn = _checkpointer_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        agent: CompiledStateGraph = create_deep_agent(  # type: ignore[type-arg]
            tools=list(tools),
            system_prompt=system_prompt,
            model=build_chat_model(),
            checkpointer=saver,
            interrupt_on=dict(interrupt_on) if interrupt_on else None,
            subagents=list(subagents) if subagents else None,
            skills=list(skill_sources) if skill_sources else None,
        )
        yield agent
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_build_agent_skills.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo/agent/graph.py tests/unit/test_build_agent_skills.py
git commit -m "feat: build_agent forwards skills sources and subagents (M4.5)"
```

---

### Task 6: Wire resolution into `run_agent`

**Files:**
- Modify: `rehketo/agent/run.py:28-29` (imports), `:223-251` (resolve + build + stream input)
- Test: `tests/integration/test_run_agent_skills.py`

The run task resolves skills, materializes doc-skill files, builds subagents on
the existing `AsyncExitStack`, and threads them through `build_agent` and the
stream input.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_run_agent_skills.py
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from fastmcp import Client, FastMCP

import rehketo.agent.run as run_mod
from rehketo.db.models import Conversation, McpServer, Run, Skill, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def test_run_agent_passes_skills_to_build_agent(
    settings_env, db_url, db, monkeypatch
) -> None:
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List PRs."""
        return "[]"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(id=uuid4(), conversation_id=conv.id, user_id=u.id,
              status="queued", model="claude-sonnet-4-6")
    db.add(run)
    srv = McpServer(id=uuid4(), name="github", url="https://x/mcp",
                    auth_token_ct=None, allowed_roles=["User"], enabled=True,
                    auto_approve=True)
    db.add(srv)
    await db.commit()
    db.add_all([
        Skill(id=uuid4(), name="github", trigger="GitHub repos", kind="mcp",
              mcp_server_id=srv.id, allowed_roles=["User"], enabled=True),
        Skill(id=uuid4(), name="policy", trigger="reimburse", kind="doc",
              instructions="# Policy", allowed_roles=["User"], enabled=True),
    ])
    await db.commit()

    captured: dict[str, Any] = {}

    class _QuietAgent:
        async def astream(self, stream_input: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            captured["stream_input"] = stream_input
            yield (AIMessageChunk(content="ok", id="m1"), {})

    async def _fake_build_agent(
        run_id: str, system_prompt: str, tools: Sequence[Any] = (),
        interrupt_on: Any = None, subagents: Any = None, skill_sources: Any = None,
    ) -> AsyncIterator[_QuietAgent]:
        captured["main_tools"] = list(tools)
        captured["subagents"] = subagents
        captured["skill_sources"] = skill_sources
        yield _QuietAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    await run_mod.run_agent(run.id, PostgresEventBus())

    assert [s["name"] for s in captured["subagents"]] == ["github"]
    assert captured["skill_sources"] == ["/skills/"]
    assert "/skills/policy/SKILL.md" in captured["stream_input"]["files"]
    # The skill-backed server's tools live ONLY on the subagent, never on the
    # main agent — that is the progressive-disclosure guarantee.
    assert captured["main_tools"] == []
    assert [t.name for t in captured["subagents"][0]["tools"]] == ["github__list_prs"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_run_agent_skills.py -v`
Expected: FAIL — `subagents`/`skill_sources` are `None` and `stream_input` has no `files` key.

- [ ] **Step 3: Add imports to `run.py`**

After line 29 (`from rehketo.mcp.servers import allowed_servers`), add:

```python
from rehketo.mcp.skills import (
    SKILLS_ROOT,
    build_skill_subagents,
    doc_skill_files,
    resolve_skills,
)
```

- [ ] **Step 4: Resolve skills in the resolve sequence**

In `run.py`, immediately after the `servers = await allowed_servers(...)` line (currently line 223), add:

```python
                resolved_skills = await resolve_skills(
                    db, user_id=user_id, roles=roles
                )
```

- [ ] **Step 5: Build subagents + thread into build_agent and stream input**

Replace the block from `async with contextlib.AsyncExitStack() as stack:` through the `stream_input` assignment (currently lines 228-251) with:

```python
            async with contextlib.AsyncExitStack() as stack:
                # A server exposed as an mcp-skill is reached ONLY by delegating
                # to its subagent — its tools must not also bind to the main
                # agent, or there is no progressive disclosure. Partition: plain
                # servers stay flat tools (unchanged behavior), skill-backed
                # servers become subagents.
                skill_server_ids = {
                    s.mcp_server_id for s in resolved_skills.mcp
                }
                plain_servers = [s for s in servers if s.id not in skill_server_ids]
                tools, interrupt_on = await build_run_toolset(
                    stack, plain_servers, run_id=str(run_id), bus=bus
                )
                # M4.5 discovery: doc-skills surface via SkillsMiddleware
                # (in-state SKILL.md files), mcp-skills via subagents the model
                # delegates to. resolve_skills already filtered to this user.
                skill_files = doc_skill_files(resolved_skills.doc)
                subagents = await build_skill_subagents(
                    stack, resolved_skills.mcp, servers, run_id=str(run_id), bus=bus
                )
                async for agent in build_agent(
                    str(run_id),
                    system_prompt,
                    tools=tools,
                    interrupt_on=interrupt_on,
                    subagents=subagents or None,
                    skill_sources=[SKILLS_ROOT] if skill_files else None,
                ):
                    config: Any = {"configurable": {"thread_id": str(run_id)}}
                    resume_cmd = (
                        await build_resume_command(agent, config, run_id=run_id)
                        if interrupt_on
                        else None
                    )
                    if resume_cmd is not None:
                        async with sessionmaker()() as db:
                            await _rehydrate_segments(db, run_id, segments)
                    # Skill files ride the initial state, not the resume Command.
                    stream_input: Any = (
                        resume_cmd
                        if resume_cmd is not None
                        else {"messages": history, "files": skill_files}
                    )
```

Note: `servers` passed to `build_skill_subagents` is the already-connected
allowed set; the builder reuses `build_run_toolset` on the same `stack`, so the
mcp-skill tools share the run's client lifecycle (closed on every exit path).

- [ ] **Step 6: Run the new test and the existing tool tests**

Run:
```bash
uv run pytest tests/integration/test_run_agent_skills.py tests/integration/test_run_agent_tools.py -v
```
Expected: the new test PASSES; the three existing tool tests still PASS (no skills seeded → `subagents=None`, `files={}`).

- [ ] **Step 7: Commit**

```bash
git add rehketo/agent/run.py tests/integration/test_run_agent_skills.py
git commit -m "feat: resolve and wire skills into agent runs (M4.5)"
```

---

### Task 7: Live end-to-end — model delegates to an mcp-skill

**Files:**
- Test: `tests/integration/test_run_agent_skills_live.py`

This is the spike's functional proof: a real deepagents graph (no fake agent),
an in-memory FastMCP server, and an assertion that delegating to the skill
subagent fires the tool and streams `tool.call`/`tool.result`. Model calls go
through the test Bifrost fixture already used by `test_run_agent_approval_live.py`
— follow that file's fixture wiring exactly.

- [ ] **Step 1: Read the live-test pattern**

Run: `sed -n '1,80p' tests/integration/test_run_agent_approval_live.py`
Note how it builds the run, seeds the Bifrost stub responses, and asserts on
`run_events`. Mirror its fixtures (`settings_env`, `db`, the bifrost stub).

- [ ] **Step 2: Write the live test**

```python
# tests/integration/test_run_agent_skills_live.py
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastmcp import Client, FastMCP
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, Skill, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus


async def test_model_delegates_to_skill_and_tool_fires(
    settings_env, db_url, db, monkeypatch, bifrost_stub
) -> None:
    # bifrost_stub: scripted assistant turns — (1) call the `task` tool with
    # subagent_type="github", (2) inside the subagent, call github__list_prs,
    # (3) final answer. Script these turns following the approval-live test's
    # stub format. The point is to assert the wiring lets a delegated tool run.
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List open PRs."""
        return "PR #1: fix bug"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(id=uuid4(), conversation_id=conv.id, user_id=u.id,
              status="queued", model="claude-sonnet-4-6")
    db.add(run)
    srv = McpServer(id=uuid4(), name="github", url="https://x/mcp",
                    auth_token_ct=None, allowed_roles=["User"], enabled=True,
                    auto_approve=True)
    db.add(srv)
    await db.commit()
    db.add(Skill(id=uuid4(), name="github", trigger="use for GitHub PRs",
                 kind="mcp", mcp_server_id=srv.id, allowed_roles=["User"],
                 enabled=True))
    await db.commit()

    await run_mod.run_agent(run.id, PostgresEventBus())

    async with sessionmaker()() as s:
        rows = (await s.execute(
            text("SELECT payload FROM run_events WHERE run_id = :r ORDER BY sequence"),
            {"r": str(run.id)},
        )).all()
    types = [r.payload["type"] for r in rows]
    assert "tool.call" in types and "tool.result" in types
    result = next(r.payload for r in rows if r.payload["type"] == "tool.result")
    assert "PR #1" in result["result"]
```

- [ ] **Step 3: Run the live test**

Run: `uv run pytest tests/integration/test_run_agent_skills_live.py -v`
Expected: PASS. If deepagents' `task` tool naming/subagent-dispatch differs
from the scripted stub, adjust the stub turns to match the real tool name the
graph exposes (inspect by logging the tools bound in a scratch run). This is
the one task where the framework's exact surface is confirmed empirically —
the spike's core uncertainty.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_run_agent_skills_live.py
git commit -m "test: live skill delegation fires MCP tool (M4.5)"
```

---

### Task 8: Tool-selection eval (the success metric)

**Files:**
- Create: `tests/eval/test_skill_discovery_lift.py`
- Create: `tests/eval/README.md`

The milestone premise is "the agent doesn't know when to use MCP servers." This
task measures it: a small prompt set that *should* trigger the skill, run twice
— baseline (skill seeded but flat tools, no subagent/skills wiring) vs. wired —
counting how often the right capability is reached. This is a measurement
harness, not a pass/fail gate; it prints a comparison.

- [ ] **Step 1: Write the eval harness**

```python
# tests/eval/test_skill_discovery_lift.py
"""Spike success metric: does wiring skills lift correct-capability use?

Not a gate — it prints baseline vs wired activation counts so the spike
checkpoint has evidence. Marked `eval` so it is opt-in like the e2e suite.
"""
from __future__ import annotations

import pytest

PROMPTS = [
    "What open PRs are on the repo?",
    "Summarize the latest pull request.",
    "Are there any code reviews waiting on me?",
    "What's our reimbursement deadline?",   # doc-skill case
    "How do I file a travel expense?",       # doc-skill case
]


@pytest.mark.eval
async def test_print_discovery_lift() -> None:
    # Implement by running run_agent twice over PROMPTS — once with skills
    # wiring disabled (monkeypatch resolve_skills to return empty) and once
    # enabled — counting runs whose run_events include a tool.call from the
    # expected server/skill. Print the two counts. Use the live bifrost stub
    # from Task 7 with non-scripted (real-model) turns if available; otherwise
    # document that this requires a live model and skip when BIFROST creds are
    # absent.
    pytest.skip("Run manually against a live model; see tests/eval/README.md")
```

- [ ] **Step 2: Document how to run it**

```markdown
<!-- tests/eval/README.md -->
# Skill-discovery eval (M4.5 spike metric)

`test_skill_discovery_lift.py` measures whether wiring skills improves how
often the agent reaches for the right capability. It needs a live model
(real Bifrost), so it is `@pytest.mark.eval` and skipped by default.

Run manually:

    uv run pytest tests/eval -m eval -s   # -s to see the printed counts

Record baseline-vs-wired counts in the spike checkpoint notes on the M4.5
branch. A meaningful lift (e.g. the doc/MCP prompts trigger the skill when
wired and do not when flat) clears the spike's bar.
```

- [ ] **Step 3: Register the `eval` marker**

Confirm `pyproject.toml`/`pytest.ini` markers. Run:
`grep -n "markers" rehketo-api/pyproject.toml`
If `eval` is not listed alongside `e2e`, add it in the same `[tool.pytest.ini_options] markers` list:
```
"eval: opt-in evaluations that require a live model",
```

- [ ] **Step 4: Verify the eval is collected and skipped cleanly**

Run: `uv run pytest tests/eval -m eval -v`
Expected: collected, 1 skipped (no unknown-marker warning).

- [ ] **Step 5: Commit**

```bash
git add tests/eval/test_skill_discovery_lift.py tests/eval/README.md rehketo-api/pyproject.toml
git commit -m "test: skill-discovery lift eval harness (M4.5 spike metric)"
```

---

### Task 9: Full validation sweep

**Files:** none (validation only)

- [ ] **Step 1: Run the rehketo-api validation block**

From `rehketo-api/`:
```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
```
Expected: all green. Fix any failure in the task that introduced it (charter
rule 5 — quote real output when claiming a step passed).

- [ ] **Step 2: Run repo-wide guards**

From repo root:
```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```
Expected: both pass.

- [ ] **Step 3: Run the contract + e2e checks if wire shapes changed**

From `rehketo-api/`:
```bash
uv run python ../tools/check_contract.py
uv run pytest -m e2e   # needs `just db`
```
Expected: pass. Slice 1 adds no new SSE event types (skill tools reuse the
existing `tool.call`/`tool.result` path), so the contract should be unchanged;
if it flags a diff, reconcile before claiming done.

- [ ] **Step 4: Final commit if any fixups were needed**

```bash
git add -A && git commit -m "chore: validation fixups for M4.5 slice 1"
```

---

## Self-review notes

- **Spec coverage:** registry table (Task 1), scope ∪ role resolution + server cross-check (Task 2), doc-skills via SkillsMiddleware (Tasks 3, 5, 6), mcp-skills via subagents (Tasks 4, 5, 6), M3.5 `interrupt_on` carried onto subagents (Task 4), success metric (Task 8). `assemble_system_prompt` deliberately unchanged per the revised spec.
- **Deferred to later slices (not in this plan):** admin CRUD route, `/settings` user-skill authoring, transcript rendering of subagent activity, enforcement beyond resolution for user-scoped authoring.
- **Empirical-confirmation point:** Task 7 Step 3 — the exact `task`-tool surface deepagents exposes for subagent delegation is confirmed by running, not assumed. This is the spike's residual unknown and is deliberately isolated to one task.
