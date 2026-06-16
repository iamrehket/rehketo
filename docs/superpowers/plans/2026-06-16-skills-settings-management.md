# Skills visibility & management in Settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the skills the agent can use for a user in Settings, let users author their own doc-skills, and give admins CRUD over global doc + mcp skills.

**Architecture:** Backend gains two routers — `/me/skills` (resolved read + self doc-skill CRUD) and `/admin/skills` (full CRUD) — mirroring the existing `/admin/mcp-servers` router. A migration relaxes name uniqueness to per-owner (partial unique indexes) so a user's skill name shadows a global of the same name; `resolve_skills` de-dupes with owned > global precedence. The UI adds one role-aware `/settings/skills` page reusing the `McpServerForm` patterns. Two new permission actions (`chat.author_skill`, `admin.manage_skills`) gate the write surfaces.

**Tech Stack:** FastAPI + SQLAlchemy (async) + Alembic + Pydantic v2; SvelteKit 5 (runes) + Tailwind v4 + Vitest; Postgres; deepagents.

**Spec:** `docs/superpowers/specs/2026-06-16-skills-settings-management-design.md`

**Branch:** `feat/skills-settings-management` (already created; the spec is committed here).

**Validation (run from the stated dir, quote real output before "done"):**
- repo root: `uv run --project rehketo-api python tools/agent_guards.py check`
- `rehketo-api/`: `uv run ruff format --check && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo && uv run lint-imports && uv run pytest && uv run python ../tools/check_contract.py`
- `rehketo-api/`: `uv run pytest -m e2e` (needs `just db`) — run after any wire-shape/UI-flow change.
- `rehketo-ui/`: `pnpm run lint && pnpm run check && pnpm run test:unit -- --run`

---

## Slice 1 — Schema + resolution foundation

Retires the two latent bugs (global-unique name; unquoted YAML frontmatter) and makes "owned shadows global" real. No UI. After this slice the agent run path is correct under per-owner names.

### Task 1: Per-owner uniqueness (migration `0015` + model)

**Files:**
- Create: `rehketo-api/alembic/versions/0015_skills_per_owner_name.py`
- Modify: `rehketo-api/rehketo/db/models.py:163` (drop `unique=True` on `name`), `:190-197` (extend `__table_args__`)
- Test: `rehketo-api/tests/integration/test_skills_model.py`

- [ ] **Step 1: Write the failing test**

Add to `rehketo-api/tests/integration/test_skills_model.py`:

```python
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from rehketo.db.models import Skill, User


def _doc(name: str, *, owner=None) -> Skill:
    return Skill(
        id=uuid4(),
        name=name,
        trigger="t",
        kind="doc",
        instructions="body",
        owner_user_id=owner,
        allowed_roles=[],
        enabled=True,
    )


async def test_user_may_reuse_a_global_name(settings_env, db_url, db) -> None:
    me = uuid4()
    db.add(User(id=me))
    await db.flush()
    db.add(_doc("research"))  # global
    db.add(_doc("research", owner=me))  # owned, same name — allowed
    await db.commit()  # must not raise


async def test_two_globals_cannot_share_a_name(settings_env, db_url, db) -> None:
    db.add(_doc("research"))
    db.add(_doc("research"))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_one_user_cannot_duplicate_their_own_name(settings_env, db_url, db) -> None:
    me = uuid4()
    db.add(User(id=me))
    await db.flush()
    db.add(_doc("research", owner=me))
    db.add(_doc("research", owner=me))
    with pytest.raises(IntegrityError):
        await db.commit()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_model.py -k "reuse_a_global or two_globals or duplicate_their_own" -v`
Expected: `test_user_may_reuse_a_global_name` FAILS with `IntegrityError` (global `UNIQUE(name)` still active).

- [ ] **Step 3: Write the migration**

Create `rehketo-api/alembic/versions/0015_skills_per_owner_name.py`:

```python
"""skills: per-owner name uniqueness (owned may shadow a global)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-16 00:00:00.000000+00:00

Replaces the global UNIQUE(name) with two partial unique indexes: globals
unique among themselves, each user unique within their own set. A plain
UNIQUE(owner_user_id, name) would not enforce global uniqueness because
Postgres treats NULLs as distinct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 0014 created UniqueConstraint("name") unnamed -> Postgres default name.
    op.drop_constraint("skills_name_key", "skills", type_="unique")
    op.create_index(
        "uq_skills_global_name",
        "skills",
        ["name"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_index(
        "uq_skills_owner_name",
        "skills",
        ["owner_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_skills_owner_name", table_name="skills")
    op.drop_index("uq_skills_global_name", table_name="skills")
    op.create_unique_constraint("skills_name_key", "skills", ["name"])
```

- [ ] **Step 4: Update the model to match**

In `rehketo-api/rehketo/db/models.py`, change the `name` column (line 163) from:

```python
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
```

to:

```python
    name: Mapped[str] = mapped_column(Text, nullable=False)
```

Add `Index` to the imports from `sqlalchemy` (it already imports `CheckConstraint`, `ForeignKey`, `Text`, etc. — add `Index` and `text`), then extend `__table_args__` (after the two existing `CheckConstraint`s):

```python
    __table_args__ = (
        CheckConstraint("kind in ('mcp','doc')", name="skills_kind_enum"),
        CheckConstraint(
            "(kind = 'mcp' AND mcp_server_id IS NOT NULL AND instructions IS NULL) "
            "OR (kind = 'doc' AND instructions IS NOT NULL AND mcp_server_id IS NULL)",
            name="skills_kind_backing",
        ),
        # Per-owner namespace: globals (owner NULL) unique among themselves;
        # each user unique within their own set; a user may reuse a global name.
        Index(
            "uq_skills_global_name",
            "name",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
        ),
        Index(
            "uq_skills_owner_name",
            "owner_user_id",
            "name",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
    )
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_model.py -v`
Expected: PASS (the test DB applies migrations, so `0015` runs; reuse allowed, both duplicate cases raise `IntegrityError`).

- [ ] **Step 6: Commit**

```bash
git add rehketo-api/alembic/versions/0015_skills_per_owner_name.py rehketo-api/rehketo/db/models.py rehketo-api/tests/integration/test_skills_model.py
git commit -m "feat: per-owner skill name uniqueness (owned may shadow global)"
```

### Task 2: `resolve_skills` — owned shadows global

**Files:**
- Modify: `rehketo-api/rehketo/mcp/skills.py:62-82`
- Test: `rehketo-api/tests/integration/test_resolve_skills.py`

- [ ] **Step 1: Write the failing test**

Add to `rehketo-api/tests/integration/test_resolve_skills.py`:

```python
async def test_owned_shadows_global_of_same_name(settings_env, db_url, db) -> None:
    me = uuid4()
    db.add(User(id=me))
    await db.flush()
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="research",
                trigger="global version",
                kind="doc",
                instructions="GLOBAL",
                allowed_roles=["User"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="research",
                trigger="my version",
                kind="doc",
                instructions="MINE",
                owner_user_id=me,
                allowed_roles=[],
                enabled=True,
            ),
        ]
    )
    await db.commit()

    resolved = await resolve_skills(db, user_id=me, roles=["User"])
    # Exactly one "research" survives, and it is the owned one.
    research = [s for s in resolved.doc if s.name == "research"]
    assert len(research) == 1
    assert research[0].instructions == "MINE"
    assert research[0].owner_user_id == me
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_resolve_skills.py -k owned_shadows -v`
Expected: FAIL — `len(research) == 2` (both rows returned, no de-dup).

- [ ] **Step 3: Implement de-dup with owned precedence**

In `rehketo-api/rehketo/mcp/skills.py`, after the `visible = [...]` list comprehension (ends line 75) and before the `allowed_ids = {...}` block, insert:

```python
    # Owned shadows global: when a user's own skill shares a name with a global
    # one, keep the owned row so exactly one card (SKILL.md file or subagent)
    # exists per name in a run — the path /skills/{name} and the subagent name
    # are both keyed by name and must not collide.
    by_name: dict[str, Skill] = {}
    for s in visible:
        current = by_name.get(s.name)
        if current is None or (
            current.owner_user_id is None and s.owner_user_id == user_id
        ):
            by_name[s.name] = s
    visible = sorted(by_name.values(), key=lambda s: s.name)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/integration/test_resolve_skills.py -v`
Expected: PASS (new test green; `test_global_role_and_owned_union` still green — its names are all distinct).

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/mcp/skills.py rehketo-api/tests/integration/test_resolve_skills.py
git commit -m "feat: resolve_skills de-dupes by name, owned shadows global"
```

### Task 3: YAML-safe doc-skill frontmatter

**Files:**
- Modify: `rehketo-api/rehketo/mcp/skills.py:88-105` (add `import json` at top of file)
- Test: `rehketo-api/tests/unit/test_skill_materialize.py`

- [ ] **Step 1: Update the existing assertions + add a hostile-trigger test**

In `rehketo-api/tests/unit/test_skill_materialize.py`, change the two assertions in `test_emits_skill_md_per_skill` from:

```python
    assert "name: policy" in content
    assert "description: reimburse" in content
```

to:

```python
    assert 'name: "policy"' in content
    assert 'description: "reimburse"' in content
```

Then append a new test:

```python
def test_trigger_with_yaml_metacharacters_round_trips() -> None:
    """A user-authored trigger with a colon, quote, or newline must not break
    SkillsMiddleware frontmatter parsing. JSON-encoding the scalar guarantees a
    well-formed YAML string."""
    hostile = 'use when: he said "hi"\nand more'
    files = doc_skill_files([_doc("policy", hostile, "body")])
    file_data = files[f"{SKILLS_ROOT}policy/SKILL.md"]
    text = file_data_to_string(file_data)  # type: ignore[arg-type]
    # frontmatter is exactly three lines between the --- fences
    front = text.split("---\n")[1]
    assert front.count("\n") == 2  # name line + description line
    # the description scalar is a single JSON-quoted token (no raw newline/colon leaks)
    import json

    assert json.dumps(hostile) in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/unit/test_skill_materialize.py -v`
Expected: FAIL — both the updated assertions and the new test fail (current code emits raw `name: policy` and an unescaped multi-line trigger).

- [ ] **Step 3: JSON-encode the frontmatter scalars**

Add `import json` to the top of `rehketo-api/rehketo/mcp/skills.py` (with the other stdlib imports). Replace the `frontmatter` line in `doc_skill_files` (line 102):

```python
        frontmatter = f"---\nname: {s.name}\ndescription: {s.trigger}\n---\n"
```

with:

```python
        # JSON-encode the scalars: JSON is a valid YAML subset, so a name or
        # trigger containing ':', '"', or a newline can't break frontmatter
        # parsing. Users author triggers now, so this is load-bearing.
        frontmatter = (
            "---\n"
            f"name: {json.dumps(s.name)}\n"
            f"description: {json.dumps(s.trigger)}\n"
            "---\n"
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/unit/test_skill_materialize.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/mcp/skills.py rehketo-api/tests/unit/test_skill_materialize.py
git commit -m "fix: JSON-encode doc-skill YAML frontmatter scalars"
```

---

## Slice 2 — Visibility (`GET /me/skills` + read-only view)

Delivers the core "no visibility" fix. After this slice every user can open `/settings/skills` and see the skills the agent can use for them.

### Task 4: New permission actions

**Files:**
- Modify: `rehketo-api/rehketo/permissions/actions.py:7-22`, `rehketo-api/rehketo/permissions/roles.py:19-31`
- Modify: `rehketo-ui/src/lib/types.ts:30-42` (Capability union + the count comment)
- Test: `rehketo-api/tests/unit/test_roles_actions.py` (create)

- [ ] **Step 1: Write the failing test**

Create `rehketo-api/tests/unit/test_roles_actions.py`:

```python
from rehketo.permissions.actions import ACTIONS_SET
from rehketo.permissions.check import permissions_for_roles


def test_new_skill_actions_declared() -> None:
    assert "chat.author_skill" in ACTIONS_SET
    assert "admin.manage_skills" in ACTIONS_SET


def test_author_skill_granted_to_all_chat_roles() -> None:
    for role in ("User", "Moderator", "Admin"):
        assert "chat.author_skill" in permissions_for_roles([role])


def test_manage_skills_is_admin_only() -> None:
    assert "admin.manage_skills" in permissions_for_roles(["Admin"])
    assert "admin.manage_skills" not in permissions_for_roles(["Moderator"])
    assert "admin.manage_skills" not in permissions_for_roles(["User"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/unit/test_roles_actions.py -v`
Expected: FAIL — actions not declared.

- [ ] **Step 3: Add the actions**

In `rehketo-api/rehketo/permissions/actions.py`, add `"chat.author_skill",` to the Chat-domain block (after `"chat.approve_tool_call",`) and `"admin.manage_skills",` to the Admin-domain block (after `"admin.manage_mcp_servers",`).

In `rehketo-api/rehketo/permissions/roles.py`, add `"chat.author_skill",` to **both** the `Moderator` and `User` frozensets (Admin already gets all actions via `frozenset(ACTIONS)`).

- [ ] **Step 4: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/unit/test_roles_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Mirror the Capability type on the UI side**

In `rehketo-ui/src/lib/types.ts`, update the comment `// The 12 canonical actions...` to `// The 14 canonical actions...` and add to the `Capability` union:

```typescript
	| 'chat.approve_tool_call'
	| 'chat.author_skill'
	| 'admin.manage_users'
	| 'admin.view_audit'
	| 'admin.manage_mcp_servers'
	| 'admin.manage_skills';
```

- [ ] **Step 6: Commit**

```bash
git add rehketo-api/rehketo/permissions/actions.py rehketo-api/rehketo/permissions/roles.py rehketo-api/tests/unit/test_roles_actions.py rehketo-ui/src/lib/types.ts
git commit -m "feat: add chat.author_skill and admin.manage_skills actions"
```

### Task 5: `GET /me/skills` endpoint

**Files:**
- Create: `rehketo-api/rehketo/api/skills_me.py`
- Modify: `rehketo-api/rehketo/main.py:112-126` (import + register router)
- Test: `rehketo-api/tests/integration/test_skills_me.py` (create)

- [ ] **Step 1: Write the failing test**

Create `rehketo-api/tests/integration/test_skills_me.py` (reuse the `_seed_session`/`_auth` helpers — copy them from `test_mcp_servers_admin.py` lines 15-35):

```python
from __future__ import annotations

from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Skill, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "User") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    return str(u.id), str(sid)


async def test_lists_global_and_owned_with_flags(settings_env, db_url, db) -> None:
    user_id, sid = await _seed_session(db)
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="policy",
                trigger="reimburse",
                kind="doc",
                instructions="body",
                allowed_roles=["User"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="mine",
                trigger="t",
                kind="doc",
                instructions="body",
                owner_user_id=UUID(user_id),
                allowed_roles=[],
                enabled=True,
            ),
        ]
    )
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/me/skills", cookies={SESSION_COOKIE: sid})
    assert r.status_code == 200
    items = {s["name"]: s for s in r.json()["items"]}
    assert items["policy"]["source"] == "global"
    assert items["policy"]["editable"] is False
    assert items["mine"]["source"] == "owned"
    assert items["mine"]["editable"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_me.py -v`
Expected: FAIL with 404 (route does not exist).

- [ ] **Step 3: Write the router**

Create `rehketo-api/rehketo/api/skills_me.py`:

```python
from __future__ import annotations

from typing import Annotated
from uuid import UUID  # noqa: TC003  # Pydantic field at runtime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.db.models import Skill
from rehketo.mcp.skills import resolve_skills
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(tags=["me"])


class MySkillOut(BaseModel):
    id: UUID
    name: str
    display_name: str | None
    kind: str
    trigger: str
    instructions: str | None
    enabled: bool
    source: str  # 'global' | 'owned'
    editable: bool


class MySkillList(BaseModel):
    items: list[MySkillOut]


def _to_out(s: Skill, *, user_id: UUID) -> MySkillOut:
    owned = s.owner_user_id == user_id
    return MySkillOut(
        id=s.id,
        name=s.name,
        display_name=s.display_name,
        kind=s.kind,
        trigger=s.trigger,
        instructions=s.instructions,
        enabled=s.enabled,
        source="owned" if owned else "global",
        editable=owned and s.kind == "doc",
    )


@router.get("/me/skills", response_model=MySkillList)
async def list_my_skills(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillList:
    resolved = await resolve_skills(db, user_id=perms.user_id, roles=perms.roles)
    rows = sorted([*resolved.doc, *resolved.mcp], key=lambda s: s.name)
    return MySkillList(items=[_to_out(s, user_id=perms.user_id) for s in rows])
```

- [ ] **Step 4: Register the router**

In `rehketo-api/rehketo/main.py`, add to the import block (near line 116) `from rehketo.api import skills_me as skills_me_api` and after `app.include_router(me_api.router)` add `app.include_router(skills_me_api.router)`.

- [ ] **Step 5: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_me.py -v`
Expected: PASS.

- [ ] **Step 6: Rebaseline the contract**

Run: `cd rehketo-api && uv run python ../tools/check_contract.py --update`
Expected: prints that the baseline was rewritten (new `/me/skills` path). Then `uv run python ../tools/check_contract.py` → "no diff".

- [ ] **Step 7: Commit**

```bash
git add rehketo-api/rehketo/api/skills_me.py rehketo-api/rehketo/main.py rehketo-api/tests/integration/test_skills_me.py rehketo-ui/openapi.snapshot.json
git commit -m "feat: GET /me/skills resolved skill list with source and editable flags"
```

### Task 6: `/settings/skills` read-only view

**Files:**
- Create: `rehketo-ui/src/routes/(app)/settings/skills/+page.ts`, `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte`
- Modify: `rehketo-ui/src/lib/types.ts` (add `MySkillOut`, `MySkillList`)
- Modify: `rehketo-ui/src/routes/(app)/settings/+page.svelte:45-86` (add a link to `/settings/skills`)

- [ ] **Step 1: Add the wire types**

In `rehketo-ui/src/lib/types.ts`, after `PreferencesOut`, add (matches `rehketo/api/skills_me.py`):

```typescript
// Matches rehketo-api/rehketo/api/skills_me.py MySkillOut.
export type MySkillOut = {
	id: string;
	name: string;
	display_name: string | null;
	kind: 'doc' | 'mcp';
	trigger: string;
	instructions: string | null;
	enabled: boolean;
	source: 'global' | 'owned';
	editable: boolean;
};

export type MySkillList = {
	items: MySkillOut[];
};
```

- [ ] **Step 2: Write the load function**

Create `rehketo-ui/src/routes/(app)/settings/skills/+page.ts` (mirrors `mcp-servers/+page.ts`):

```typescript
import { error, redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { ApiError, type MySkillList } from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const mine = await apiFetch<MySkillList>('/me/skills', { skipAuthRedirect: true });
		return { skills: mine.items };
	} catch (err) {
		if (err instanceof ApiError) {
			if (err.status === 401) {
				const next = encodeURIComponent(url.pathname + url.search);
				throw redirect(302, `/login?next=${next}`);
			}
			throw error(err.status || 500, err.message);
		}
		throw err;
	}
};
```

- [ ] **Step 3: Write the page (read-only cards only for now)**

Create `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte`:

```svelte
<script lang="ts">
	import type { MySkillOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	// svelte-ignore state_referenced_locally
	let skills = $state<MySkillOut[]>(data.skills);
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">Skills</h1>
	<p class="mt-1 text-sm text-muted">
		Capabilities the assistant can discover and use on your behalf.
	</p>

	<section class="mt-6">
		<h2 class="text-sm font-semibold">Skills available to you</h2>
		<ul class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
			{#each skills as skill (skill.id)}
				<li class="rounded-md border border-border bg-surface p-3">
					<div class="flex items-center justify-between gap-2">
						<span class="font-mono text-sm">{skill.display_name ?? skill.name}</span>
						<span class="rounded bg-bg px-1.5 py-0.5 text-xs text-muted">{skill.kind}</span>
					</div>
					<p class="mt-1 text-xs text-muted">{skill.trigger}</p>
					<p class="mt-2 text-xs text-muted">
						{skill.source === 'owned' ? 'your skill' : 'global · read-only'}
					</p>
				</li>
			{:else}
				<li class="text-sm text-muted">No skills available yet.</li>
			{/each}
		</ul>
	</section>
</div>
```

- [ ] **Step 4: Link it from the main settings page**

In `rehketo-ui/src/routes/(app)/settings/+page.svelte`, after the custom-instructions `</section>` (line 76) and before the `{#if auth.can('admin.manage_mcp_servers')}` block, add:

```svelte
	<section class="mt-8">
		<h2 class="text-sm font-semibold">Skills</h2>
		<a href="/settings/skills" class="mt-2 inline-block text-sm text-accent hover:underline">
			View your skills →
		</a>
	</section>
```

- [ ] **Step 5: Verify lint/types/build**

Run: `cd rehketo-ui && pnpm run check && pnpm run lint`
Expected: no errors.

- [ ] **Step 6: Run the e2e suite (wire shape + new route added)**

Run: `cd rehketo-api && uv run pytest -m e2e` (start `just db` first)
Expected: existing flows still PASS (no regression).

- [ ] **Step 7: Commit**

```bash
git add rehketo-ui/src/routes/'(app)'/settings/skills rehketo-ui/src/lib/types.ts rehketo-ui/src/routes/'(app)'/settings/+page.svelte
git commit -m "feat: /settings/skills read-only view of available skills"
```

---

## Slice 3 — User doc-skill authoring

After this slice a user can create, edit, and delete their own doc-skills on `/settings/skills`.

### Task 7: `/me/skills` write CRUD (doc-only, self-owned)

**Files:**
- Modify: `rehketo-api/rehketo/api/skills_me.py`
- Test: `rehketo-api/tests/integration/test_skills_me.py`

- [ ] **Step 1: Write the failing tests**

Append to `rehketo-api/tests/integration/test_skills_me.py` (add the `_auth` helper from `test_mcp_servers_admin.py` lines 31-35, and have `_seed_session` also return the csrf token — change its return to `return str(u.id), str(sid), issue_csrf_token(str(sid))` and update the Task-5 test's unpacking):

```python
_DOC_BODY = {"name": "my-notes", "trigger": "use for my notes", "instructions": "Steps."}


async def test_create_edit_delete_own_doc_skill(settings_env, db_url, db) -> None:
    user_id, sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))
        assert r.status_code == 201
        created = r.json()
        assert created["kind"] == "doc"
        assert created["source"] == "owned"
        assert created["editable"] is True
        skill_id = created["id"]

        r = await c.patch(
            f"/me/skills/{skill_id}", json={"trigger": "updated"}, **_auth(sid, csrf)
        )
        assert r.status_code == 200
        assert r.json()["trigger"] == "updated"

        r = await c.delete(f"/me/skills/{skill_id}", **_auth(sid, csrf))
        assert r.status_code == 204


async def test_cannot_edit_a_global_skill(settings_env, db_url, db) -> None:
    user_id, sid, csrf = await _seed_session(db)
    glob = Skill(
        id=uuid4(),
        name="policy",
        trigger="t",
        kind="doc",
        instructions="b",
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(glob)
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.patch(
            f"/me/skills/{glob.id}", json={"trigger": "x"}, **_auth(sid, csrf)
        )
        assert r.status_code == 404  # not owned -> not found
        r = await c.delete(f"/me/skills/{glob.id}", **_auth(sid, csrf))
        assert r.status_code == 404


async def test_duplicate_own_name_is_409(settings_env, db_url, db) -> None:
    user_id, sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))).status_code == 201
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(sid, csrf))
        assert r.status_code == 409


async def test_author_without_capability_is_403(settings_env, db_url, db) -> None:
    # A user seeded with no roles holds no actions, including chat.author_skill.
    u = User(id=uuid4(), display_name="No", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    csrf = issue_csrf_token(str(sid))
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/me/skills", json=_DOC_BODY, **_auth(str(sid), csrf))
        assert r.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_me.py -v`
Expected: FAIL — POST/PATCH/DELETE return 405/404 (only GET exists).

- [ ] **Step 3: Implement the write handlers**

In `rehketo-api/rehketo/api/skills_me.py`, first adjust the existing import lines (edit in place — do **not** add duplicate `from` lines, ruff rejects them):
- add `from datetime import UTC, datetime` (new line at top with the other stdlib imports)
- change `from uuid import UUID  # noqa: TC003 ...` → `from uuid import UUID, uuid4  # noqa: TC003  # Pydantic field at runtime`
- change `from fastapi import APIRouter, Depends` → `from fastapi import APIRouter, Depends, HTTPException, Response`
- change `from pydantic import BaseModel` → `from pydantic import BaseModel, Field`
- add `from sqlalchemy import and_, select`

Then add the module constant, schemas, and handlers:

```python
_NAME_PATTERN = r"^[a-z0-9]+([_-][a-z0-9]+)*$"


class MySkillCreate(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    trigger: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1)
    enabled: bool = True


class MySkillPatch(BaseModel):
    # name + kind are identity — not patchable. enabled toggles inline.
    display_name: str | None = None
    trigger: str | None = Field(default=None, min_length=1, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


async def _get_owned_doc_or_404(db: AsyncSession, skill_id: UUID, user_id: UUID) -> Skill:
    s = (
        await db.execute(
            select(Skill).where(
                and_(
                    Skill.id == skill_id,
                    Skill.owner_user_id == user_id,
                    Skill.kind == "doc",
                )
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return s


@router.post("/me/skills", status_code=201, response_model=MySkillOut)
async def create_my_skill(
    payload: MySkillCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillOut:
    perms.require("chat.author_skill", resource_type="skill", resource_id=None)
    dup = (
        await db.execute(
            select(Skill.id).where(
                and_(Skill.owner_user_id == perms.user_id, Skill.name == payload.name)
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="skill name already exists")
    skill = Skill(
        id=uuid4(),
        name=payload.name,
        display_name=payload.display_name,
        trigger=payload.trigger,
        kind="doc",
        instructions=payload.instructions,
        owner_user_id=perms.user_id,
        allowed_roles=[],
        enabled=payload.enabled,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill, user_id=perms.user_id)


@router.patch("/me/skills/{skill_id}", response_model=MySkillOut)
async def patch_my_skill(
    skill_id: UUID,
    payload: MySkillPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillOut:
    perms.require("chat.author_skill", resource_type="skill", resource_id=skill_id)
    skill = await _get_owned_doc_or_404(db, skill_id, perms.user_id)
    if "display_name" in payload.model_fields_set:
        skill.display_name = payload.display_name
    if payload.trigger is not None:
        skill.trigger = payload.trigger
    if payload.instructions is not None:
        skill.instructions = payload.instructions
    if payload.enabled is not None:
        skill.enabled = payload.enabled
    skill.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill, user_id=perms.user_id)


@router.delete("/me/skills/{skill_id}", status_code=204)
async def delete_my_skill(
    skill_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require("chat.author_skill", resource_type="skill", resource_id=skill_id)
    skill = await _get_owned_doc_or_404(db, skill_id, perms.user_id)
    await db.delete(skill)
    await db.commit()
    return Response(status_code=204)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_me.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Rebaseline the contract**

Run: `cd rehketo-api && uv run python ../tools/check_contract.py --update && uv run python ../tools/check_contract.py`
Expected: baseline rewritten (POST/PATCH/DELETE `/me/skills`), then "no diff".

- [ ] **Step 6: Commit**

```bash
git add rehketo-api/rehketo/api/skills_me.py rehketo-api/tests/integration/test_skills_me.py rehketo-ui/openapi.snapshot.json
git commit -m "feat: self-service doc-skill authoring at /me/skills"
```

### Task 8: `SkillForm` + "Your skills" section

**Files:**
- Create: `rehketo-ui/src/lib/skill-form.ts`, `rehketo-ui/src/lib/components/SkillForm.svelte`
- Create: `rehketo-ui/src/lib/skill-form.spec.ts`, `rehketo-ui/src/lib/components/SkillForm.dom.spec.ts`
- Modify: `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte`

- [ ] **Step 1: Write the patch-body builder test**

Create `rehketo-ui/src/lib/skill-form.spec.ts`:

```typescript
import { describe, expect, it } from 'vitest';

import {
	buildAdminCreateBody,
	buildAdminPatchBody,
	buildSkillPatchBody,
	type AdminSkillFormState,
	type SkillFormState
} from './skill-form';

const base: SkillFormState = {
	displayName: '',
	trigger: 'use when X',
	instructions: 'do Y',
	enabled: true
};

describe('buildSkillPatchBody', () => {
	it('sends trigger, instructions, enabled, and display_name (null when blank)', () => {
		expect(buildSkillPatchBody(base)).toEqual({
			display_name: null,
			trigger: 'use when X',
			instructions: 'do Y',
			enabled: true
		});
	});

	it('forwards a non-blank display_name', () => {
		expect(buildSkillPatchBody({ ...base, displayName: 'My Notes' }).display_name).toBe('My Notes');
	});
});

const adminBase: AdminSkillFormState = {
	name: 'policy',
	kind: 'doc',
	displayName: '',
	trigger: 'reimburse',
	instructions: 'Steps.',
	mcpServerId: '',
	allowedRoles: ['User'],
	enabled: true
};

describe('buildAdminCreateBody', () => {
	it('sends instructions and omits mcp_server_id for a doc skill', () => {
		const body = buildAdminCreateBody(adminBase);
		expect(body).toMatchObject({ name: 'policy', kind: 'doc', instructions: 'Steps.' });
		expect('mcp_server_id' in body).toBe(false);
	});

	it('sends mcp_server_id and omits instructions for an mcp skill', () => {
		const body = buildAdminCreateBody({
			...adminBase,
			kind: 'mcp',
			instructions: '',
			mcpServerId: 'srv-1'
		});
		expect(body).toMatchObject({ kind: 'mcp', mcp_server_id: 'srv-1' });
		expect('instructions' in body).toBe(false);
	});
});

describe('buildAdminPatchBody', () => {
	it('never sends name or kind, and sends only the matching backing field', () => {
		const body = buildAdminPatchBody({ ...adminBase, kind: 'mcp', mcpServerId: 'srv-2' });
		expect('name' in body).toBe(false);
		expect('kind' in body).toBe(false);
		expect(body.mcp_server_id).toBe('srv-2');
		expect('instructions' in body).toBe(false);
	});
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/skill-form.spec.ts`
Expected: FAIL — module not found.

- [ ] **Step 3: Write `skill-form.ts`**

Create `rehketo-ui/src/lib/skill-form.ts`:

```typescript
// Bodies for the doc-skill author surface (rehketo-api/rehketo/api/skills_me.py).
// name + kind are identity and never sent on PATCH. display_name is sent as
// null when blank so a user can clear it.
export type MySkillCreateBody = {
	name: string;
	display_name: string | null;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export type MySkillPatchBody = {
	display_name: string | null;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export type SkillFormState = {
	displayName: string;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export function buildSkillPatchBody(state: SkillFormState): MySkillPatchBody {
	return {
		display_name: state.displayName || null,
		trigger: state.trigger,
		instructions: state.instructions,
		enabled: state.enabled
	};
}

// Admin surface (rehketo-api/rehketo/api/skills_admin.py). One form authors
// both doc and mcp global skills, so the body carries only the backing field
// that matches `kind` (mirrors the DB skills_kind_backing check).
export type AdminSkillCreateBody = {
	name: string;
	display_name: string | null;
	kind: 'doc' | 'mcp';
	trigger: string;
	allowed_roles: string[];
	enabled: boolean;
	instructions?: string;
	mcp_server_id?: string;
};

export type AdminSkillPatchBody = {
	display_name: string | null;
	trigger: string;
	allowed_roles: string[];
	enabled: boolean;
	instructions?: string;
	mcp_server_id?: string;
};

export type AdminSkillFormState = {
	name?: string;
	kind: 'doc' | 'mcp';
	displayName: string;
	trigger: string;
	instructions: string;
	mcpServerId: string;
	allowedRoles: string[];
	enabled: boolean;
};

export function buildAdminCreateBody(s: AdminSkillFormState): AdminSkillCreateBody {
	const body: AdminSkillCreateBody = {
		name: s.name ?? '',
		display_name: s.displayName || null,
		kind: s.kind,
		trigger: s.trigger,
		allowed_roles: s.allowedRoles,
		enabled: s.enabled
	};
	if (s.kind === 'doc') body.instructions = s.instructions;
	else body.mcp_server_id = s.mcpServerId;
	return body;
}

export function buildAdminPatchBody(s: AdminSkillFormState): AdminSkillPatchBody {
	// kind + name are identity (not patchable); send only the backing field
	// matching the existing kind.
	const body: AdminSkillPatchBody = {
		display_name: s.displayName || null,
		trigger: s.trigger,
		allowed_roles: s.allowedRoles,
		enabled: s.enabled
	};
	if (s.kind === 'doc') body.instructions = s.instructions;
	else body.mcp_server_id = s.mcpServerId;
	return body;
}
```

- [ ] **Step 4: Write `SkillForm.svelte`**

Create `rehketo-ui/src/lib/components/SkillForm.svelte` — one form for both the user doc surface (`variant="user"`, default) and the admin doc+mcp surface (`variant="admin"`), modeled on `McpServerForm.svelte`:

```svelte
<script lang="ts">
	import {
		buildAdminCreateBody,
		buildAdminPatchBody,
		buildSkillPatchBody,
		type AdminSkillCreateBody,
		type AdminSkillPatchBody,
		type MySkillCreateBody,
		type MySkillPatchBody
	} from '$lib/skill-form';
	import type { AdminSkillOut, McpServerOut, MySkillOut } from '$lib/types';

	// Source of truth for roles: rehketo-api/rehketo/permissions/roles.py.
	const ROLES = ['Admin', 'Moderator', 'User'];

	let {
		skill = null,
		variant = 'user',
		servers = [],
		busy = false,
		onSubmit,
		onCancel
	}: {
		skill?: MySkillOut | AdminSkillOut | null;
		variant?: 'user' | 'admin';
		servers?: McpServerOut[];
		busy?: boolean;
		onSubmit: (
			body: MySkillCreateBody | MySkillPatchBody | AdminSkillCreateBody | AdminSkillPatchBody
		) => void;
		onCancel?: () => void;
	} = $props();

	const isEdit = $derived(skill !== null);
	const isAdmin = $derived(variant === 'admin');
	const uid = $derived(skill ? `skill-${skill.id}` : 'skill');

	// svelte-ignore state_referenced_locally
	let name = $state(skill?.name ?? '');
	// svelte-ignore state_referenced_locally
	let displayName = $state(skill?.display_name ?? '');
	// svelte-ignore state_referenced_locally
	let trigger = $state(skill?.trigger ?? '');
	// svelte-ignore state_referenced_locally
	let instructions = $state(skill?.instructions ?? '');
	// svelte-ignore state_referenced_locally
	let enabled = $state(skill?.enabled ?? true);
	// kind: user authoring is always 'doc'; admin chooses on create, fixed on edit.
	// svelte-ignore state_referenced_locally
	let kind = $state<'doc' | 'mcp'>(skill?.kind ?? 'doc');
	// Admin-only fields — present only on AdminSkillOut.
	// svelte-ignore state_referenced_locally
	let allowedRoles = $state<string[]>(
		skill && 'allowed_roles' in skill ? [...skill.allowed_roles] : [...ROLES]
	);
	// svelte-ignore state_referenced_locally
	let mcpServerId = $state(skill && 'mcp_server_id' in skill ? (skill.mcp_server_id ?? '') : '');

	const isDoc = $derived(kind === 'doc');

	function submit(): void {
		if (isAdmin) {
			const state = { name, kind, displayName, trigger, instructions, mcpServerId, allowedRoles, enabled };
			onSubmit(skill ? buildAdminPatchBody(state) : buildAdminCreateBody(state));
		} else if (skill) {
			onSubmit(buildSkillPatchBody({ displayName, trigger, instructions, enabled }));
		} else {
			onSubmit({ name, display_name: displayName || null, trigger, instructions, enabled });
		}
	}

	const canSubmit = $derived.by(() => {
		if (!isEdit && !name) return false;
		if (!trigger) return false;
		return isDoc ? Boolean(instructions) : Boolean(mcpServerId);
	});
</script>

<div class="flex flex-col gap-3">
	<label class="text-xs text-muted" for={`${uid}-name`}>Name</label>
	<input
		id={`${uid}-name`}
		data-field="name"
		bind:value={name}
		readonly={isEdit}
		placeholder={isEdit ? undefined : 'my-notes'}
		class="rounded-md border border-border p-2 text-sm {isEdit ? 'bg-surface text-muted' : 'bg-bg'}"
	/>

	{#if isAdmin}
		<label class="text-xs text-muted" for={`${uid}-kind`}>Kind</label>
		{#if isEdit}
			<span class="font-mono text-sm text-muted">{kind}</span>
		{:else}
			<select
				id={`${uid}-kind`}
				data-field="kind"
				bind:value={kind}
				class="rounded-md border border-border bg-bg p-2 text-sm"
			>
				<option value="doc">doc</option>
				<option value="mcp">mcp</option>
			</select>
		{/if}
	{/if}

	<label class="text-xs text-muted" for={`${uid}-display`}>Display name (optional)</label>
	<input
		id={`${uid}-display`}
		data-field="display-name"
		bind:value={displayName}
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	<label class="text-xs text-muted" for={`${uid}-trigger`}>Use when…</label>
	<input
		id={`${uid}-trigger`}
		data-field="trigger"
		bind:value={trigger}
		placeholder="the user asks about my project notes"
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	{#if isDoc}
		<label class="text-xs text-muted" for={`${uid}-instructions`}>Instructions</label>
		<textarea
			id={`${uid}-instructions`}
			data-field="instructions"
			bind:value={instructions}
			rows="6"
			class="resize-y rounded-md border border-border bg-bg p-2 text-sm"
		></textarea>
	{:else}
		<label class="text-xs text-muted" for={`${uid}-server`}>MCP server</label>
		<select
			id={`${uid}-server`}
			data-field="mcp-server"
			bind:value={mcpServerId}
			class="rounded-md border border-border bg-bg p-2 text-sm"
		>
			<option value="">— choose a server —</option>
			{#each servers as srv (srv.id)}
				<option value={srv.id}>{srv.name}</option>
			{/each}
		</select>
	{/if}

	{#if isAdmin}
		<fieldset class="flex gap-4 text-sm">
			<legend class="text-xs text-muted">Allowed roles</legend>
			{#each ROLES as role (role)}
				<label class="flex items-center gap-1">
					<input type="checkbox" value={role} bind:group={allowedRoles} />
					{role}
				</label>
			{/each}
		</fieldset>
	{/if}

	<label class="flex items-center gap-2 text-sm">
		<input data-field="enabled" type="checkbox" bind:checked={enabled} />
		Enabled
	</label>

	<div class="flex justify-end gap-2">
		{#if isEdit}
			<button
				type="button"
				data-action="cancel"
				onclick={() => onCancel?.()}
				class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-hover"
			>
				Cancel
			</button>
		{/if}
		<button
			type="button"
			data-action="submit"
			onclick={submit}
			disabled={busy || !canSubmit}
			class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
		>
			{isEdit ? 'Save' : 'Add skill'}
		</button>
	</div>
</div>
```

- [ ] **Step 5: Write the DOM spec**

Create `rehketo-ui/src/lib/components/SkillForm.dom.spec.ts` (mirror `McpServerForm.dom.spec.ts` — open that file to match its render/query helpers). Minimum coverage:

```typescript
import { render } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import SkillForm from './SkillForm.svelte';

describe('SkillForm', () => {
	it('disables submit until name, trigger, and instructions are present (create)', () => {
		const { getByText } = render(SkillForm, { props: { onSubmit: vi.fn() } });
		expect((getByText('Add skill') as HTMLButtonElement).disabled).toBe(true);
	});

	it('makes the name field read-only in edit mode', () => {
		const skill = {
			id: '1',
			name: 'my-notes',
			display_name: null,
			kind: 'doc' as const,
			trigger: 't',
			instructions: 'i',
			enabled: true,
			source: 'owned' as const,
			editable: true
		};
		const { container } = render(SkillForm, { props: { skill, onSubmit: vi.fn() } });
		const nameInput = container.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameInput.readOnly).toBe(true);
	});

	it('admin variant shows a kind selector and swaps instructions for a server picker', () => {
		const servers = [
			{
				id: 's1',
				name: 'github',
				url: 'https://x/mcp',
				has_auth_token: false,
				allowed_roles: ['User'],
				enabled: true,
				auto_approve: false,
				created_at: '',
				updated_at: ''
			}
		];
		const { container, getByText } = render(SkillForm, {
			props: { variant: 'admin', servers, onSubmit: vi.fn() }
		});
		const kindSelect = container.querySelector('[data-field="kind"]') as HTMLSelectElement;
		expect(kindSelect).not.toBeNull();
		// doc by default -> instructions present, no server picker
		expect(container.querySelector('[data-field="instructions"]')).not.toBeNull();
		expect(container.querySelector('[data-field="mcp-server"]')).toBeNull();
		// submit is disabled until name/trigger/instructions are filled
		expect((getByText('Add skill') as HTMLButtonElement).disabled).toBe(true);
	});
});
```

- [ ] **Step 6: Wire "Your skills" into the page**

Update `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte` to add create/edit/delete. Replace the whole `<script>` and add the section after "Skills available to you":

```svelte
<script lang="ts">
	import { apiFetch } from '$lib/api';
	import SkillForm from '$lib/components/SkillForm.svelte';
	import type { MySkillCreateBody, MySkillPatchBody } from '$lib/skill-form';
	import { auth } from '$lib/stores/auth.svelte';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type MySkillOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	// svelte-ignore state_referenced_locally
	let skills = $state<MySkillOut[]>(data.skills);
	let editingId = $state<string | null>(null);
	let createBusy = $state(false);
	let editBusy = $state(false);

	let available = $derived(skills);
	let mine = $derived(skills.filter((s) => s.editable));

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} skill.` });
		}
	}

	async function create(body: MySkillCreateBody): Promise<void> {
		createBusy = true;
		try {
			const created = await apiFetch<MySkillOut>('/me/skills', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			skills = [created, ...skills];
			toasts.push({ variant: 'info', message: 'Skill added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			createBusy = false;
		}
	}

	async function save(skill: MySkillOut, body: MySkillPatchBody): Promise<void> {
		editBusy = true;
		try {
			const updated = await apiFetch<MySkillOut>(`/me/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			skills = skills.map((s) => (s.id === updated.id ? updated : s));
			editingId = null;
			toasts.push({ variant: 'info', message: 'Skill updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			editBusy = false;
		}
	}

	async function remove(skill: MySkillOut): Promise<void> {
		if (!confirm(`Delete skill "${skill.name}"?`)) return;
		try {
			await apiFetch(`/me/skills/${skill.id}`, { method: 'DELETE' });
			skills = skills.filter((s) => s.id !== skill.id);
		} catch (err) {
			fail('delete', err);
		}
	}
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">Skills</h1>
	<p class="mt-1 text-sm text-muted">
		Capabilities the assistant can discover and use on your behalf.
	</p>

	<section class="mt-6">
		<h2 class="text-sm font-semibold">Skills available to you</h2>
		<ul class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
			{#each available as skill (skill.id)}
				<li class="rounded-md border border-border bg-surface p-3">
					<div class="flex items-center justify-between gap-2">
						<span class="font-mono text-sm">{skill.display_name ?? skill.name}</span>
						<span class="rounded bg-bg px-1.5 py-0.5 text-xs text-muted">{skill.kind}</span>
					</div>
					<p class="mt-1 text-xs text-muted">{skill.trigger}</p>
					<p class="mt-2 text-xs text-muted">
						{skill.source === 'owned' ? 'your skill' : 'global · read-only'}
					</p>
				</li>
			{:else}
				<li class="text-sm text-muted">No skills available yet.</li>
			{/each}
		</ul>
	</section>

	{#if auth.can('chat.author_skill')}
		<section class="mt-8">
			<h2 class="text-sm font-semibold">Your skills</h2>
			<ul class="mt-3 flex flex-col gap-3">
				{#each mine as skill (skill.id)}
					<li class="rounded-md border border-border bg-surface p-3">
						<div class="flex items-center justify-between gap-3">
							<span class="font-mono text-sm">{skill.name}</span>
							<div class="flex gap-2">
								<button
									type="button"
									data-action="edit"
									onclick={() => (editingId = editingId === skill.id ? null : skill.id)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									Edit
								</button>
								<button
									type="button"
									data-action="delete"
									onclick={() => remove(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
								>
									Delete
								</button>
							</div>
						</div>
						{#if editingId === skill.id}
							<div class="mt-3 border-t border-border pt-3">
								<SkillForm
									{skill}
									busy={editBusy}
									onSubmit={(body) => save(skill, body as MySkillPatchBody)}
									onCancel={() => (editingId = null)}
								/>
							</div>
						{/if}
					</li>
				{:else}
					<li class="text-sm text-muted">You haven't created any skills.</li>
				{/each}
			</ul>

			<div class="mt-4 rounded-md border border-border bg-surface p-4">
				<h3 class="text-sm font-semibold">New skill</h3>
				<div class="mt-3">
					<SkillForm
						skill={null}
						busy={createBusy}
						onSubmit={(body) => create(body as MySkillCreateBody)}
					/>
				</div>
			</div>
		</section>
	{/if}
</div>
```

- [ ] **Step 7: Run UI tests + checks**

Run: `cd rehketo-ui && pnpm run test:unit -- --run && pnpm run check && pnpm run lint`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add rehketo-ui/src/lib/skill-form.ts rehketo-ui/src/lib/skill-form.spec.ts rehketo-ui/src/lib/components/SkillForm.svelte rehketo-ui/src/lib/components/SkillForm.dom.spec.ts rehketo-ui/src/routes/'(app)'/settings/skills/+page.svelte
git commit -m "feat: user doc-skill authoring UI on /settings/skills"
```

---

## Slice 4 — Admin management

After this slice an admin sees a "Manage global / mcp skills" section on `/settings/skills` with full CRUD over global doc and mcp skills.

### Task 9: `/admin/skills` router

**Files:**
- Create: `rehketo-api/rehketo/api/skills_admin.py`
- Modify: `rehketo-api/rehketo/main.py` (import + register router)
- Test: `rehketo-api/tests/integration/test_skills_admin.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `rehketo-api/tests/integration/test_skills_admin.py` (copy `_seed_session`/`_auth` from `test_mcp_servers_admin.py`):

```python
from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import McpServer, User, UserRole
from rehketo.main import create_app


async def _seed_session(db, role: str = "Admin") -> tuple[str, str]:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.flush()
    db.add(UserRole(user_id=u.id, role=role))
    await db.commit()
    sid = await create_session(
        db, user_id=u.id, identity_provider="entra", refresh_token="rt", ttl_minutes=60
    )
    return str(sid), issue_csrf_token(str(sid))


def _auth(sid: str, csrf: str) -> dict:
    return {
        "cookies": {SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        "headers": {CSRF_HEADER: csrf},
    }


_DOC = {"name": "policy", "kind": "doc", "trigger": "reimburse", "instructions": "Steps."}


async def test_admin_doc_crud_roundtrip(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        assert r.status_code == 201
        skill_id = r.json()["id"]
        assert r.json()["kind"] == "doc"

        r = await c.get("/admin/skills", cookies={SESSION_COOKIE: sid})
        assert skill_id in [s["id"] for s in r.json()["items"]]

        r = await c.patch(
            f"/admin/skills/{skill_id}", json={"enabled": False}, **_auth(sid, csrf)
        )
        assert r.json()["enabled"] is False

        r = await c.delete(f"/admin/skills/{skill_id}", **_auth(sid, csrf))
        assert r.status_code == 204


async def test_admin_create_mcp_skill(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )
    db.add(srv)
    await db.commit()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/admin/skills",
            json={
                "name": "github",
                "kind": "mcp",
                "trigger": "repos",
                "mcp_server_id": str(srv.id),
                "allowed_roles": ["User"],
            },
            **_auth(sid, csrf),
        )
        assert r.status_code == 201
        assert r.json()["mcp_server_id"] == str(srv.id)


async def test_kind_backing_xor_is_422(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # doc kind with no instructions
        r = await c.post(
            "/admin/skills",
            json={"name": "bad", "kind": "doc", "trigger": "t"},
            **_auth(sid, csrf),
        )
        assert r.status_code == 422
        # mcp kind with instructions instead of a server
        r = await c.post(
            "/admin/skills",
            json={"name": "bad2", "kind": "mcp", "trigger": "t", "instructions": "x"},
            **_auth(sid, csrf),
        )
        assert r.status_code == 422


async def test_non_admin_is_403(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db, role="User")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/admin/skills", cookies={SESSION_COOKIE: sid})).status_code == 403
        assert (await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))).status_code == 403


async def test_duplicate_global_name_is_409(settings_env, db_url, db) -> None:
    sid, csrf = await _seed_session(db)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))).status_code == 201
        r = await c.post("/admin/skills", json=_DOC, **_auth(sid, csrf))
        assert r.status_code == 409
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_admin.py -v`
Expected: FAIL — 404 (router missing).

- [ ] **Step 3: Write the router**

Create `rehketo-api/rehketo/api/skills_admin.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.db.models import Skill
from rehketo.permissions.check import known_roles
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(prefix="/admin/skills", tags=["admin"])

_NAME_PATTERN = r"^[a-z0-9]+([_-][a-z0-9]+)*$"
_KNOWN_ROLES = known_roles()


def _validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted(set(roles) - _KNOWN_ROLES)
    if unknown:
        raise ValueError(f"unknown role(s): {', '.join(unknown)}")
    return roles


class AdminSkillCreate(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    kind: str
    trigger: str = Field(min_length=1, max_length=2000)
    instructions: str | None = None
    mcp_server_id: UUID | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str]) -> list[str]:
        return _validate_roles(v)

    @model_validator(mode="after")
    def kind_backing(self) -> AdminSkillCreate:
        # Mirror the DB skills_kind_backing check at the boundary so a bad shape
        # is a clean 422, not a 500 from the failed INSERT.
        if self.kind == "doc":
            if not self.instructions or self.mcp_server_id is not None:
                raise ValueError("doc skills require instructions and no mcp_server_id")
        elif self.kind == "mcp":
            if self.mcp_server_id is None or self.instructions is not None:
                raise ValueError("mcp skills require mcp_server_id and no instructions")
        else:
            raise ValueError("kind must be 'doc' or 'mcp'")
        return self


class AdminSkillPatch(BaseModel):
    # name + kind are identity — not patchable.
    display_name: str | None = None
    trigger: str | None = Field(default=None, min_length=1, max_length=2000)
    instructions: str | None = None
    mcp_server_id: UUID | None = None
    allowed_roles: list[str] | None = None
    enabled: bool | None = None

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validate_roles(v)


class AdminSkillOut(BaseModel):
    id: UUID
    name: str
    display_name: str | None
    kind: str
    trigger: str
    instructions: str | None
    mcp_server_id: UUID | None
    owner_user_id: UUID | None
    allowed_roles: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AdminSkillList(BaseModel):
    items: list[AdminSkillOut]


def _to_out(s: Skill) -> AdminSkillOut:
    return AdminSkillOut(
        id=s.id,
        name=s.name,
        display_name=s.display_name,
        kind=s.kind,
        trigger=s.trigger,
        instructions=s.instructions,
        mcp_server_id=s.mcp_server_id,
        owner_user_id=s.owner_user_id,
        allowed_roles=s.allowed_roles,
        enabled=s.enabled,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _get_or_404(db: AsyncSession, skill_id: UUID) -> Skill:
    s = (
        await db.execute(select(Skill).where(Skill.id == skill_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return s


@router.get("", response_model=AdminSkillList)
async def list_skills(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillList:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=None)
    rows = (await db.execute(select(Skill).order_by(Skill.name))).scalars().all()
    return AdminSkillList(items=[_to_out(s) for s in rows])


@router.post("", status_code=201, response_model=AdminSkillOut)
async def create_skill(
    payload: AdminSkillCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillOut:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=None)
    # Global namespace (owner_user_id IS NULL).
    dup = (
        await db.execute(
            select(Skill.id).where(
                and_(Skill.owner_user_id.is_(None), Skill.name == payload.name)
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="skill name already exists")
    skill = Skill(
        id=uuid4(),
        name=payload.name,
        display_name=payload.display_name,
        kind=payload.kind,
        trigger=payload.trigger,
        instructions=payload.instructions,
        mcp_server_id=payload.mcp_server_id,
        owner_user_id=None,
        allowed_roles=payload.allowed_roles,
        enabled=payload.enabled,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill)


@router.patch("/{skill_id}", response_model=AdminSkillOut)
async def patch_skill(
    skill_id: UUID,
    payload: AdminSkillPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillOut:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=skill_id)
    skill = await _get_or_404(db, skill_id)
    if "display_name" in payload.model_fields_set:
        skill.display_name = payload.display_name
    if payload.trigger is not None:
        skill.trigger = payload.trigger
    if "instructions" in payload.model_fields_set and skill.kind == "doc":
        skill.instructions = payload.instructions
    if payload.mcp_server_id is not None and skill.kind == "mcp":
        skill.mcp_server_id = payload.mcp_server_id
    if payload.allowed_roles is not None:
        skill.allowed_roles = payload.allowed_roles
    if payload.enabled is not None:
        skill.enabled = payload.enabled
    skill.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=skill_id)
    skill = await _get_or_404(db, skill_id)
    await db.delete(skill)
    await db.commit()
    return Response(status_code=204)
```

- [ ] **Step 4: Register the router**

In `rehketo-api/rehketo/main.py`, add `from rehketo.api import skills_admin as skills_admin_api` to the import block and `app.include_router(skills_admin_api.router)` to the registration block.

- [ ] **Step 5: Run to verify it passes**

Run: `cd rehketo-api && uv run pytest tests/integration/test_skills_admin.py -v`
Expected: PASS.

- [ ] **Step 6: Rebaseline the contract**

Run: `cd rehketo-api && uv run python ../tools/check_contract.py --update && uv run python ../tools/check_contract.py`
Expected: baseline rewritten (the `/admin/skills` paths), then "no diff".

- [ ] **Step 7: Commit**

```bash
git add rehketo-api/rehketo/api/skills_admin.py rehketo-api/rehketo/main.py rehketo-api/tests/integration/test_skills_admin.py rehketo-ui/openapi.snapshot.json
git commit -m "feat: admin skill CRUD at /admin/skills"
```

### Task 10: Admin section on `/settings/skills`

**Files:**
- Modify: `rehketo-ui/src/lib/types.ts` (add `AdminSkillOut`, `AdminSkillList`)
- Modify: `rehketo-ui/src/routes/(app)/settings/skills/+page.ts` (also load admin data when capable)
- Modify: `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte` (admin section)
- Modify: `rehketo-ui/src/routes/(app)/settings/+page.svelte` (admin link to skills)

- [ ] **Step 1: Add the admin wire types**

In `rehketo-ui/src/lib/types.ts`, after `MySkillList`, add:

```typescript
// Matches rehketo-api/rehketo/api/skills_admin.py AdminSkillOut.
export type AdminSkillOut = {
	id: string;
	name: string;
	display_name: string | null;
	kind: 'doc' | 'mcp';
	trigger: string;
	instructions: string | null;
	mcp_server_id: string | null;
	owner_user_id: string | null;
	allowed_roles: string[];
	enabled: boolean;
	created_at: string;
	updated_at: string;
};

export type AdminSkillList = {
	items: AdminSkillOut[];
};
```

- [ ] **Step 2: Extend the load function**

Update `rehketo-ui/src/routes/(app)/settings/skills/+page.ts` to conditionally load admin data. Replace the `load` body:

```typescript
import { error, redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { auth } from '$lib/stores/auth.svelte';
import {
	ApiError,
	type AdminSkillList,
	type McpServerList,
	type MySkillList
} from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const mine = await apiFetch<MySkillList>('/me/skills', { skipAuthRedirect: true });
		// Admin extras: only fetch when the capability is present (auth store is
		// hydrated by the root layout before page loads run).
		if (auth.can('admin.manage_skills')) {
			const [allSkills, servers] = await Promise.all([
				apiFetch<AdminSkillList>('/admin/skills', { skipAuthRedirect: true }),
				apiFetch<McpServerList>('/admin/mcp-servers', { skipAuthRedirect: true })
			]);
			return { skills: mine.items, adminSkills: allSkills.items, servers: servers.items };
		}
		return { skills: mine.items, adminSkills: null, servers: null };
	} catch (err) {
		if (err instanceof ApiError) {
			if (err.status === 401) {
				const next = encodeURIComponent(url.pathname + url.search);
				throw redirect(302, `/login?next=${next}`);
			}
			throw error(err.status || 500, err.message);
		}
		throw err;
	}
};
```

- [ ] **Step 3: Add the admin section to the page**

In `rehketo-ui/src/routes/(app)/settings/skills/+page.svelte`, add the admin types to `<script>` (the `SkillForm` component is already imported from Task 8):

```svelte
	import type { AdminSkillCreateBody, AdminSkillPatchBody } from '$lib/skill-form';
	import type { AdminSkillOut, McpServerOut } from '$lib/types';
```

Add state + handlers inside `<script>` (after the `mine` derived). The admin surface reuses `SkillForm variant="admin"`, so the handlers take the typed bodies the form builds — no inline `FormData` parsing:

```svelte
	// svelte-ignore state_referenced_locally
	let adminSkills = $state<AdminSkillOut[]>(data.adminSkills ?? []);
	const servers: McpServerOut[] = data.servers ?? [];
	let adminEditingId = $state<string | null>(null);
	let adminCreateBusy = $state(false);
	let adminEditBusy = $state(false);

	async function adminCreate(body: AdminSkillCreateBody): Promise<void> {
		adminCreateBusy = true;
		try {
			const created = await apiFetch<AdminSkillOut>('/admin/skills', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			adminSkills = [created, ...adminSkills];
			toasts.push({ variant: 'info', message: 'Skill created.' });
		} catch (err) {
			fail('create', err);
		} finally {
			adminCreateBusy = false;
		}
	}

	async function adminSave(skill: AdminSkillOut, body: AdminSkillPatchBody): Promise<void> {
		adminEditBusy = true;
		try {
			const updated = await apiFetch<AdminSkillOut>(`/admin/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			adminSkills = adminSkills.map((s) => (s.id === updated.id ? updated : s));
			adminEditingId = null;
			toasts.push({ variant: 'info', message: 'Skill updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			adminEditBusy = false;
		}
	}

	async function adminToggle(skill: AdminSkillOut): Promise<void> {
		try {
			const updated = await apiFetch<AdminSkillOut>(`/admin/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ enabled: !skill.enabled })
			});
			adminSkills = adminSkills.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}

	async function adminRemove(skill: AdminSkillOut): Promise<void> {
		if (!confirm(`Delete global skill "${skill.name}"?`)) return;
		try {
			await apiFetch(`/admin/skills/${skill.id}`, { method: 'DELETE' });
			adminSkills = adminSkills.filter((s) => s.id !== skill.id);
		} catch (err) {
			fail('delete', err);
		}
	}
```

Add the section to the markup after the "Your skills" `{/if}`:

```svelte
	{#if auth.can('admin.manage_skills')}
		<section class="mt-8">
			<h2 class="text-sm font-semibold">Manage global / mcp skills</h2>
			<ul class="mt-3 flex flex-col gap-3">
				{#each adminSkills as skill (skill.id)}
					<li class="rounded-md border border-border bg-surface p-3">
						<div class="flex items-center justify-between gap-3">
							<div>
								<span class="font-mono text-sm">{skill.name}</span>
								<span class="ml-2 rounded bg-bg px-1.5 py-0.5 text-xs text-muted">{skill.kind}</span>
								{#if !skill.enabled}<span class="ml-2 text-xs text-muted">disabled</span>{/if}
								<p class="text-xs text-muted">{skill.trigger}</p>
								{#if skill.allowed_roles.length}
									<p class="text-xs text-muted">roles: {skill.allowed_roles.join(', ')}</p>
								{/if}
							</div>
							<div class="flex gap-2">
								<button
									type="button"
									data-action="admin-edit"
									onclick={() => (adminEditingId = adminEditingId === skill.id ? null : skill.id)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									Edit
								</button>
								<button
									type="button"
									data-action="admin-toggle"
									onclick={() => adminToggle(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									{skill.enabled ? 'Disable' : 'Enable'}
								</button>
								<button
									type="button"
									data-action="admin-delete"
									onclick={() => adminRemove(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
								>
									Delete
								</button>
							</div>
						</div>
						{#if adminEditingId === skill.id}
							<div class="mt-3 border-t border-border pt-3">
								<SkillForm
									variant="admin"
									{skill}
									{servers}
									busy={adminEditBusy}
									onSubmit={(body) => adminSave(skill, body as AdminSkillPatchBody)}
									onCancel={() => (adminEditingId = null)}
								/>
							</div>
						{/if}
					</li>
				{:else}
					<li class="text-sm text-muted">No global skills configured.</li>
				{/each}
			</ul>

			<div class="mt-4 rounded-md border border-border bg-surface p-4">
				<h3 class="text-sm font-semibold">New global skill</h3>
				<div class="mt-3">
					<SkillForm
						variant="admin"
						skill={null}
						{servers}
						busy={adminCreateBusy}
						onSubmit={(body) => adminCreate(body as AdminSkillCreateBody)}
					/>
				</div>
			</div>
		</section>
	{/if}
```

- [ ] **Step 4: Add the admin link on the main settings page**

In `rehketo-ui/src/routes/(app)/settings/+page.svelte`, inside the existing `{#if auth.can('admin.manage_mcp_servers')}` Administration section, add a second link below the MCP servers link:

```svelte
			<a href="/settings/skills" class="mt-2 block text-sm text-accent hover:underline">
				Manage skills →
			</a>
```

- [ ] **Step 5: Run UI checks**

Run: `cd rehketo-ui && pnpm run check && pnpm run lint && pnpm run test:unit -- --run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rehketo-ui/src/lib/types.ts rehketo-ui/src/routes/'(app)'/settings/skills rehketo-ui/src/routes/'(app)'/settings/+page.svelte
git commit -m "feat: admin skill management section on /settings/skills"
```

---

## Final: full validation sweep

- [ ] **Step 1: Repo guards + contract**

Run (repo root): `uv run --project rehketo-api python tools/agent_guards.py check && uv run --project rehketo-api python tools/sync_agent_rules.py --check`
Expected: PASS.

- [ ] **Step 2: API suite**

Run (`rehketo-api/`): `uv run ruff format --check && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo && uv run lint-imports && uv run pytest && uv run python ../tools/check_contract.py`
Expected: all PASS, contract "no diff".

- [ ] **Step 3: e2e (start `just db` first)**

Run (`rehketo-api/`): `uv run pytest -m e2e`
Expected: PASS (no regression in existing flows).

- [ ] **Step 4: UI suite**

Run (`rehketo-ui/`): `pnpm run lint && pnpm run check && pnpm run test:unit -- --run`
Expected: PASS.

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "feat: skills visibility & management in settings" --body "Implements docs/superpowers/specs/2026-06-16-skills-settings-management-design.md"
```

---

## Notes for the executor

- **Test DB applies migrations.** The `db`/`db_url` fixtures run Alembic, so `0015` is exercised by every integration test — no separate "run the migration" step.
- **`skipAuthRedirect` exists** on `apiFetch` (see `mcp-servers/+page.ts`); use it in load functions so a 401 is handled by the page, not the fetch wrapper.
- **Globbed paths in `git add`** must quote the `(app)` route group (parentheses are shell globs): `'(app)'`.
- **The contract is rebaselined per endpoint-adding task.** If `check_contract.py` shows an unexpected diff at the final sweep, a `--update` was missed — re-run it and inspect the diff before committing.
- **Out of scope (do not build):** user-authored mcp-skills, transcript rendering of subagent activity, the Settings design refresh.
