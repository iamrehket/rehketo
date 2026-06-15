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
- **Activation reveals; M3.5 approval still governs calling.** Activating a
  skill makes its tools available; when an activated MCP tool actually fires,
  it still flows through the existing `interrupt_on` / `auto_approve` gate.
  The two concerns stay orthogonal.
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

## Runtime flow

**Resolution (in `run_agent()`, extending today's resolve→assemble→build
sequence):**

1. Resolve roles + user id (exists today).
2. `resolve_skills(user_id, roles)` → *global skills the user is role-allowed*
   ∪ *skills the user owns* (`owner_user_id = user_id`). Each `kind='mcp'`
   skill is cross-checked against `allowed_servers()` so a card is never shown
   for a server the user cannot run; disabled skills and disabled backing
   servers are filtered out.
3. Build skill cards → `assemble_system_prompt(custom_instructions, skills)`.
4. Bind `activate_skill` plus today's base tools. Skill tools are **not**
   pre-bound.

**Prompt assembly (extends the M2 seam).** `assemble_system_prompt()` gains a
`skills` parameter and appends a Skills section of cards — `name` + `trigger`
only, no schemas or doc bodies:

```
## Skills
You have skills you can activate when a task calls for one. To use a skill,
call activate_skill(name) — this loads its tools and instructions.
- github — use when working with GitHub repos, PRs, issues, or code review
- expense-policy — use when answering questions about reimbursement or travel spend
```

**The `activate_skill(name)` meta-tool.** Always bound — the one tool that is
always present. Dispatch by kind:

- **doc-skill** → returns the `instructions` body as the tool result, in
  context for the rest of the run. No new tools needed.
- **mcp-skill (Approach C, primary)** → spins up a subagent scoped to that
  server's tools, seeded with a system prompt from the skill's
  trigger/instructions; the subagent does the work and returns its result.
  Skill tools never bloat the main agent's binding.

**Approach A probe (decided in the spike).** A branch where `activate_skill`
instead appends to an `active_skills` field in graph state, and a dynamic
model node rebinds the main agent's tools on the next step — "true" in-run
disclosure. The spec commits to **C as the fallback-safe default**; A is
adopted for v1 only if the spike shows deepagents exposes a clean
dynamic-model seam at acceptable latency and tool-selection quality.

## Integration points

All changes extend existing seams; no parallel paths.

- `rehketo/db/models.py` — new `Skill` model + migration.
- `rehketo/agent/prompt.py` — `assemble_system_prompt()` gains `skills`; adds
  the Skills card section.
- `rehketo/agent/run.py` — add the `resolve_skills(...)` call to the resolve
  sequence.
- `rehketo/mcp/skills.py` (new) — `resolve_skills()` (scope ∪ role math,
  server cross-check) and the `activate_skill` tool factory + subagent
  construction.
- `rehketo/agent/graph.py` — bind `activate_skill`; house the Approach-A
  dynamic-model probe behind a clearly-marked seam.
- Later slices: `rehketo/api/` admin route for global skills; `/settings` UI
  for user-owned skills.

## Build sequence

1. **Slice 1 — the spike (risk retirement).** `Skill` model + migration; seed
   one MCP-skill and one doc-skill directly in the DB (no UI). Implement
   `resolve_skills`, cards in the prompt, `activate_skill` via subagent (C),
   plus the A probe. Validate end-to-end and measure the tool-selection lift.
   **Checkpoint: decide C vs A for v1.**
2. **Slice 2 — global skills productionized.** Admin CRUD route, role gating
   enforced, e2e coverage, transcript rendering of activation events.
3. **Slice 3 — user-scoped skills.** Authoring surface for `owner_user_id`
   skills (`/settings`), scope enforcement in resolution. *(Spec-aware now,
   built here.)*

## Testing & success criteria

- **Unit:** `resolve_skills` scope/role math (global ∪ owned, server
  cross-check, disabled filtering); `assemble_system_prompt` card rendering;
  `activate_skill` dispatch for both kinds.
- **e2e (offline browser suite):** a run where the model activates a skill and
  uses its tool — guards the wire shapes the AGENTS.md validation block calls
  out (run `pytest -m e2e` whenever these shapes change).
- **Success metric (spike):** a small eval set of prompts that *should*
  trigger a skill. Compare baseline (today: flat tools) against the skill-card
  prompt on activation rate / correct tool use. The milestone's premise is
  "the agent doesn't know when" — the bar is a measurable lift in reaching for
  the right capability, not merely "it runs."
