# Skills with progressive discovery (M4.5)

**Date:** 2026-06-15
**Status:** Approved design. Adds M4.5 to
`2026-06-10-roadmap-family-launch-design.md` — a "discovery" peer to the
shipped M3.5 "approval".

## What and why

M3/M3.5 shipped MCP tool calling and per-call approval, but in practice the
agent does not reach for those tools. The base system prompt is `"You are a
helpful assistant."` and every role-allowed server's tools arrive as a flat,
unannotated pile — the model has the capability but no map of *when* a
capability is relevant. The MCP investment sits underused, and onboarding
family (M8) onto an agent that ignores its tools has little value.

M4.5 introduces **skills**: a unified, DB-backed registry of capabilities the
agent can discover and activate. Each skill carries a `trigger` ("use
when…") line that is always cheaply visible, while its tools and instructions
load only on demand. This is progressive discovery — the model sees a short
map of what it *could* do, and pulls in the detail only when a task calls for
it.

A skill is backed by either an MCP server (v1: one skill = one server) or an
authored markdown doc. The two share one table and one activation path, so
discovery works the same regardless of backing.

## Scope decisions

- **Spec the full feature; build incrementally, spike first.** The first
  build slice is a risk-retiring spike (below). The full vision — registry,
  global and user-scoped skills, authoring surfaces — is captured here so
  nothing is retrofitted, but only the spike is built before the
  C-vs-A activation decision is made.
- **One skill = one MCP server for v1.** Activating an MCP-backed skill
  reveals all of that server's tools. The schema leaves room to later attach
  an explicit tool subset (bundle skills), but bundling is not built until a
  concrete case demands it (charter rule 3).
- **Unified `skills` table across both backings.** `kind` discriminates
  `'mcp'` from `'doc'`. This is the abstraction the "unify both" goal asks
  for; it is earned because two concrete backings exist on day one (MCP
  servers today, authored docs new), not speculative.
- **Scope is global or user-owned, spec-aware now, enforced later.** The
  `owner_user_id` column (`NULL` = global) and the run-time resolution rule
  exist from the first migration. The authoring/association surface for
  user-scoped skills is a later slice — the column and rule are present so
  user skills need no schema change when built.
- **Build on deepagents' native primitives, not a hand-rolled meta-tool.**
  deepagents already ships `SkillsMiddleware` (Anthropic-style skills with
  progressive disclosure — name+description in the prompt, full `SKILL.md`
  body read on demand) and `subagents=` (each `SubAgent` carries `name`,
  `description`, scoped `tools`, and `interrupt_on`; the framework auto-adds
  the delegation tool). These cover the doc-skill and MCP-skill cases
  respectively. We do **not** build a custom `activate_skill` meta-tool —
  doing so would re-invent two framework primitives and fight the framework
  (charter rule 3). The earlier "Approach C vs. Approach A" question is
  retired by this: deepagents provides the subagent-delegation path (C) and
  native skill disclosure directly, so the custom dynamic-model-rebind (A) is
  unnecessary.
- **Discovery reveals; M3.5 approval still governs calling.** Surfacing a
  skill card or delegating to a subagent makes its tools reachable; when an
  MCP tool actually fires it still flows through the existing `interrupt_on` /
  `auto_approve` gate, carried onto the `SubAgent` via its `interrupt_on`
  field. The two concerns stay orthogonal.
- **`trigger` lives on the skill row, not on `mcp_servers`.** `mcp_servers`
  stays about *connection*; `skills` owns *discovery* metadata. This keeps a
  single source of discovery truth once bundle skills arrive.
- **Out of scope:** tool-bundle skills (subset of a server's tools, or
  spanning servers); authoring UI for global skills (admin route is a later
  slice, not the spike); semantic/embedding-based skill ranking (cards are
  static text); nested skill activation (a subagent activating further
  skills).

## Data model

A single `skills` table (new Alembic migration `0014`, after `0013`):

| Column | Purpose |
|---|---|
| `id` | PK (uuid) |
| `name` | unique slug; the argument to `activate_skill(name)` and the card key |
| `display_name` | human label for cards / UI |
| `trigger` | the "use when…" text; the heart of discovery, always shown on the card |
| `kind` | `'mcp'` \| `'doc'` |
| `mcp_server_id` | FK → `mcp_servers.id`, set when `kind='mcp'` (v1: one skill ⇄ one server) |
| `instructions` | markdown body, set when `kind='doc'`; injected on activation |
| `owner_user_id` | nullable FK → users; `NULL` = global, else user-scoped |
| `allowed_roles` | JSONB role gate, reused from the `mcp_servers` pattern; applies to global skills |
| `enabled` | on/off |
| `created_at` / `updated_at` | timestamps |

Integrity expectations (validated at the write boundary, not over-engineered):
`kind='mcp'` requires `mcp_server_id` and leaves `instructions` null;
`kind='doc'` requires `instructions` and leaves `mcp_server_id` null.

## Framework alignment

deepagents (the existing agent runtime) ships the two primitives this feature
needs, so M4.5 wires our registry into them rather than re-implementing them:

- **`SkillsMiddleware`** — loads skills (a `SKILL.md` per skill: YAML
  frontmatter `name`/`description` + markdown body) from a pluggable
  *backend* and injects metadata into the system prompt, with the full body
  read on demand. This is progressive disclosure, native. The backend is a
  virtual-filesystem protocol (`read`/`write`/`glob`/`ls`); `StateBackend`
  stores those files in ephemeral agent state. The DB stays the source of
  truth — at run start we *materialize* the resolved doc-skills into a
  `StateBackend`, so nothing skill-related lives on a real filesystem.
- **`subagents=` on `create_deep_agent`** — each `SubAgent` is a dict with
  `name`, `description`, `system_prompt`, scoped `tools`, and `interrupt_on`.
  deepagents auto-adds a delegation tool; the main agent sees only the
  subagents' names+descriptions and delegates by description. Skill tools are
  scoped to the subagent, never bound to the main agent — progressive
  disclosure of *tools*, native.

## Runtime flow

**Resolution (in `run_agent()`, extending today's resolve→assemble→build
sequence):**

1. Resolve roles + user id (exists today).
2. `resolve_skills(user_id, roles)` → *global skills the user is role-allowed*
   ∪ *skills the user owns* (`owner_user_id = user_id`). Each `kind='mcp'`
   skill is cross-checked against `allowed_servers()` so a skill is never
   offered for a server the user cannot run; disabled skills and disabled
   backing servers are filtered out.
3. Split the resolved skills by `kind` and feed them to the two deepagents
   primitives below.

**doc-skills → `SkillsMiddleware` over a `StateBackend`.** For each resolved
`kind='doc'` skill, write a `SKILL.md` (frontmatter `name` = our `name`,
`description` = our `trigger`; body = `instructions`) into a `StateBackend`,
then pass `SkillsMiddleware(backend=…, sources=[…])` into `create_deep_agent`.
The middleware renders the cards and handles on-demand body reads. No
custom prompt section and no `activate_skill` tool.

**mcp-skills → `subagents=`.** For each resolved `kind='mcp'` skill, build a
`SubAgent` dict: `name` = our `name`, `description` = our `trigger`,
`system_prompt` seeded from the trigger (and any future skill instructions),
`tools` = that server's adapted `StructuredTool`s (from the existing
`build_run_toolset` adapter path), `interrupt_on` = the per-tool M3.5 config
for that server. Pass the list via `create_deep_agent(subagents=…)`.

**Base prompt.** `assemble_system_prompt(custom_instructions)` is unchanged —
the skills surface comes entirely from `SkillsMiddleware` and the subagent
delegation tool, so the M2 seam keeps its single responsibility.

## Integration points

All changes extend existing seams; no parallel paths.

- `rehketo/db/models.py` — new `Skill` model + migration `0014`.
- `rehketo/mcp/skills.py` (new) — `resolve_skills()` (scope ∪ role math,
  server cross-check) returning resolved doc-skills and mcp-skills; helpers to
  materialize doc-skills into a `StateBackend` and to build `SubAgent` dicts
  from mcp-skills (reusing the `build_run_toolset` adapter for tools).
- `rehketo/agent/run.py` — call `resolve_skills(...)` in the resolve sequence
  and thread the doc-backend + subagents into the agent build.
- `rehketo/agent/graph.py` — `build_agent()` gains `subagents` and a skills
  `backend`/`sources` parameter, forwarded to `create_deep_agent` (with
  `SkillsMiddleware` installed when doc-skills exist).
- `rehketo/agent/prompt.py` — unchanged (recorded here so a reviewer does not
  expect a change that the framework made unnecessary).
- Later slices: `rehketo/api/` admin route for global skills; `/settings` UI
  for user-owned skills.

## Build sequence

1. **Slice 1 — the spike (risk retirement).** `Skill` model + migration; seed
   one MCP-skill and one doc-skill directly in the DB (no UI). Implement
   `resolve_skills`, doc-skill `StateBackend` materialization + `SkillsMiddleware`
   wiring, and mcp-skill `SubAgent` construction. Validate end-to-end and
   measure the tool-selection lift. The framework already retires the
   activation-mechanism risk, so the spike's question is narrowed to: *does
   wiring our registry into these primitives measurably improve when the agent
   reaches for the right capability?*
2. **Slice 2 — global skills productionized.** Admin CRUD route, role gating
   enforced, e2e coverage, transcript rendering of skill/subagent activity.
3. **Slice 3 — user-scoped skills.** Authoring surface for `owner_user_id`
   skills (`/settings`), scope enforcement in resolution. *(Spec-aware now,
   built here.)*

## Testing & success criteria

- **Unit:** `resolve_skills` scope/role math (global ∪ owned, server
  cross-check, disabled filtering); doc-skill `SKILL.md` materialization
  (frontmatter shape, body); `SubAgent` construction from an mcp-skill
  (description = trigger, tools from the adapter, `interrupt_on` carried).
- **Integration:** a `run_agent` run where the model delegates to an mcp-skill
  subagent and its tool fires — reuse the in-memory FastMCP transport pattern
  from `test_run_agent_tools.py`; assert the `tool.call`/`tool.result` events
  still stream.
- **e2e (offline browser suite):** a run that exercises a skill end-to-end —
  guards the wire shapes the AGENTS.md validation block calls out (run
  `pytest -m e2e` whenever these shapes change).
- **Success metric (spike):** a small eval set of prompts that *should*
  trigger a skill. Compare baseline (today: flat tools) against the
  skills-wired agent on activation rate / correct tool use. The milestone's
  premise is "the agent doesn't know when" — the bar is a measurable lift in
  reaching for the right capability, not merely "it runs."
