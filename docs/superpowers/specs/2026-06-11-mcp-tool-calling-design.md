# MCP tool calling, host role (M3)

**Date:** 2026-06-11
**Status:** Approved design. Implements M3 of
`2026-06-10-roadmap-family-launch-design.md`.

## What and why

Rehketo becomes an MCP **host**: a generic MCP client connects to
admin-configured external servers over streamable HTTP, their tools flow into
the `tools=[]` seam in `rehketo/agent/graph.py`, and tool-call/tool-result
events stream over SSE and render in the transcript. Authorization is a
deliberately simple per-server, per-role allowlist enforced through the
existing permission gate.

The server list is data, never code (north star): servers live in a database
table managed live from an admin page — no config files, no restart to
reconfigure.

## Scope decisions

- **HTTP transport only.** A server row is a URL plus an optional bearer
  token. No stdio: spawning admin-configured commands as API child processes
  is a sharper security edge and an in-process lifecycle burden that M4 would
  immediately have to move. Local servers are reachable at localhost URLs.
- **Allowlist granularity is per-server.** A role granted a server gets all
  of its tools. Per-tool granularity is a roadmap follow-up, not built now
  (charter rule 3).
- **Tool calls auto-execute.** Allowlisted = trusted; the admin allowlist is
  the safety boundary. Per-call user approval (with a per-server flag) is the
  new roadmap item M3.5.
- **Client library is fastmcp.** The roadmap already names it as the future
  framework for built-in tools — one MCP library, one path. The MCP-to-
  LangChain adapter is owned code (~50 lines), which we mostly need anyway
  because the event-publishing wrapper is ours regardless.
  `langchain-mcp-adapters` was rejected to avoid a second, overlapping
  MCP-ecosystem dependency.
- **Out of scope:** MCP apps (M13, renumbered from M6 in the 2026-06-11 roadmap
  revision), user-authored servers, per-user server configuration, MCP resources
  and prompts (tools only).

## Schema (migration 0011)

New table `mcp_servers`:

| column          | type        | notes                                              |
| --------------- | ----------- | -------------------------------------------------- |
| `id`            | UUID        | PK                                                  |
| `name`          | TEXT        | NOT NULL UNIQUE; slug of alnum segments joined by single `_`/`-` (no `__`, which is the tool-prefix separator), max 64; used as tool prefix |
| `url`           | TEXT        | NOT NULL; streamable-HTTP endpoint                  |
| `auth_token_ct` | BYTEA       | nullable; Fernet ciphertext (follows `sessions.refresh_token_ct`) |
| `allowed_roles` | JSONB       | NOT NULL; list of role-name strings                 |
| `enabled`       | BOOLEAN     | NOT NULL; soft kill-switch                          |
| `created_at`    | timestamptz | NOT NULL DEFAULT now()                              |
| `updated_at`    | timestamptz | NOT NULL DEFAULT now(), set on every write          |

`allowed_roles` is JSONB-on-the-row, not a join table: roles are plain strings
in `ROLE_PERMISSIONS`, not DB entities; a join table would invent a
normalization the rest of the system doesn't have. `auth_token` uses the
existing `rehketo.auth.crypto` envelope encryption — the same treatment as
refresh tokens.

## Admin API (new `rehketo/api/mcp_servers.py`)

- `GET /admin/mcp-servers` → list of servers. `auth_token` is never returned;
  responses carry `has_auth_token: bool` instead.
- `POST /admin/mcp-servers` → create.
- `PATCH /admin/mcp-servers/{id}` → partial update. `auth_token` is settable
  (and clearable with `null`), never readable.
- `DELETE /admin/mcp-servers/{id}` → delete. Disabling via `enabled=false` is
  the normal way to take a server offline; delete is for mistakes.

All four require the new action `admin.manage_mcp_servers` (added to
`ACTIONS`, granted to `Admin` in `ROLE_PERMISSIONS`), checked via
`permissions.require(...)` with `resource_type="mcp_server"` and the row id
(or `None` for list/create) as `resource_id`. Pydantic request/response
models live next to the router. URL validation is boundary-only: a syntactic
http(s)-URL check at the API; reachability is discovered at run time, not
configuration time. CSRF is covered by the existing mutating-method
middleware. The OpenAPI snapshot is rebaselined and
`tools/check_contract.py` must pass.

## Permissions: one gate, the body grows

New action `chat.use_mcp_server`, granted to every role that has
`chat.write` (today: `Admin`, `Moderator`, `User`). `check_permission` gains an optional keyword:

```python
def check_permission(
    roles, action, *, resource_type, resource_id,
    resource_roles: Iterable[str] | None = None,
) -> bool
```

When `resource_roles` is provided, the gate additionally requires a non-empty
intersection between the caller's roles and `resource_roles`. The call site
passes the server row's `allowed_roles`; the gate stays a pure function — DB
access stays at call sites. A server that fails the check contributes no
tools to the run.

This keeps the single-gate charter rule intact (no parallel allowlist check
growing elsewhere — the roadmap's standing concern) and preserves the OpenFGA
contract: at cutover, `allowed_roles` becomes relationship tuples and only
this module's body changes.

## Runtime (new package `rehketo/mcp/`)

One responsibility per file:

- **`servers.py`** — loads enabled `mcp_servers` rows for a run and filters
  them through `check_permission` with the run user's roles.
- **`adapter.py`** — converts one MCP tool to a LangChain `StructuredTool`:
  name `{server.name}__{tool.name}` (collision-proof across servers),
  description passed through, and the MCP `inputSchema` passed directly as
  `args_schema` — langchain-core ≥ 1.4 accepts a JSON-schema dict, so no
  pydantic model generation. The tool coroutine publishes `tool.call`,
  invokes `client.call_tool(...)`, and publishes `tool.result`.
- **`registry.py`** — orchestrates per run: rows → one fastmcp `Client` per
  server (`StreamableHttpTransport(url, auth=<decrypted token>)`) →
  `list_tools()` → adapted `StructuredTool` list. Client construction lives
  in a single seam function so tests can inject a memory-transport client.

**Connection lifecycle is per-run.** `run_agent` builds the registry at run
start, passes the tools into `build_agent(..., tools=tools)` — the `tools=[]`
literal becomes a parameter, keeping `build_agent` a pure function of its
inputs — and closes the clients in the existing `finally` block. No shared
state across requests or processes: the property M1 established and the M4
worker split depends on. The cost is one HTTP handshake plus `list_tools`
per server per run; at family scale that is the right side of the trade.

Import-linter contracts: `rehketo.mcp` may depend on `rehketo.db`,
`rehketo.permissions`, `rehketo.core`, `rehketo.config`, and `rehketo.runs`
(the event bus); `rehketo.agent` may depend on `rehketo.mcp`; `rehketo.api`
does not import `rehketo.mcp` (the admin routes touch only `db` +
`permissions`).

Implementation notes: `rehketo.mcp` also depends on `rehketo.auth` (the
Fernet helpers decrypt `auth_token_ct`) and invokes the gate through
`ResolvedPermissions` — constructed directly with the run user's id, since
the run task sits outside FastAPI DI. The api→mcp import contract is
direct-only (`allow_indirect_imports`): the chain api → agent.run → mcp is
legitimate. The registry also validates each combined `{server}__{tool}`
name against the provider tool-name contract (`^[a-zA-Z0-9_-]{1,64}$`,
fullmatch), skipping offenders, bounds each server's connect+list_tools at
10s, and treats client-close failures like connect failures — a tool server
dying mid-run must not change the run's outcome.

## SSE events

Two new types in the stable event schema — the shapes the v1 spec sketched:

```json
{"type": "tool.call",   "run_id": "...", "sequence": 7, "call_id": "...",
 "tool": "github__search_issues", "arguments": {"query": "..."}}
{"type": "tool.result", "run_id": "...", "sequence": 8, "call_id": "...",
 "result": "...", "is_error": false}
```

Events are published by the adapter wrapper — not parsed out of LangGraph's
message stream — so the SSE schema stays decoupled from LangGraph internals.
They flow through the durable bus like every other event: persisted to
`run_events`, resumable via `from_sequence`, no new machinery.

The `result` string in the **event** is truncated at 16 KB (with a marker);
the full result still goes back to the model. The cap protects the bus and
the UI from a tool that returns megabytes.

Implementation note (discovered in planning): LangGraph executes parallel
tool calls concurrently, so a run can have several publishers racing the
bus's `MAX(sequence)+1` insert. `PostgresEventBus.publish` serializes
publishes with a process-local per-run `asyncio.Lock` (all of a run's
publishers share one process, today and after the M4 worker split).

## Transcript reload

Tool events are durable in `run_events`, but conversation reload renders
`messages` rows, so tool chips would otherwise vanish on refresh. Decision:
**reconstruct from `run_events`** — the existing conversation transcript
endpoint (`GET` on the conversation's messages) additionally fetches
`tool.call`/`tool.result` events for the conversation's runs and returns a
chronologically interleaved transcript whose items carry a discriminator
(message vs. tool activity); exact pydantic shape is plan detail. No double-write, no schema change; the
event log M1 built is the single source of truth for live streaming, resume,
and now reload. (Rejected: live-only display loses history; persisting tool
calls as `messages` rows writes the same fact twice.)

## UI

- **Types:** `tool.call` / `tool.result` join the `RunEvent` union in
  `src/lib/types.ts`.
- **Stream handling:** `subscribeRun` in `src/lib/sse.ts` gains `onToolCall`
  / `onToolResult` handlers.
- **Transcript:** a collapsed **tool chip** rendered chronologically between
  assistant text segments — tool name plus a running spinner that resolves to
  success/failure, expandable to show arguments and result JSON. Dark
  workbench styling; no new design language.
- **Admin page:** a server-management page following the M2 settings-page
  pattern, visible only when `/me/capabilities` includes
  `admin.manage_mcp_servers` (capabilities come from the API — never
  reconstructed in the frontend): server list, create form (name, URL,
  token, role multi-select, enabled toggle), per-row enable/disable toggle,
  and delete with confirmation. Editing an existing server's URL, token, or
  roles from the UI is deferred — use delete+recreate or PATCH via API in the
  interim. Tool chips render adjacent to the run's assistant message; live
  streaming and reload both use the same chronological ordering from
  `run_events`, so they are mutually consistent.

## Error handling

Boundary-only (charter rule 4):

- **Server unreachable or `list_tools` fails at run start** → log a warning,
  skip that server, run proceeds with the remaining tools. A broken tool
  server must not take chat down: chat is core, tools are enhancement.
- **Tool call raises or returns an MCP error** → `tool.result` with
  `is_error: true`; the error text is returned to the model as the tool
  result (standard LangChain behavior) so the agent can recover or explain.
- No retries, no circuit breakers, no per-server timeout knobs — fastmcp
  defaults until a real failure mode demands otherwise.

## Testing

- **Unit:** adapter conversion (schema passthrough, name prefixing, event
  publishing order), `check_permission` with `resource_roles` (granted,
  denied, empty list, action without resource_roles unchanged), result
  truncation.
- **Integration (pytest, real postgres):** admin CRUD routes including
  token write-only behavior and role gating; end-to-end run with an
  in-process fastmcp server injected through the client seam — assert the
  SSE event sequence, `run_events` rows, and transcript reload contents.
  Unreachable-server test asserts the run still succeeds.
- **Contract:** OpenAPI snapshot rebaselined; `check_contract.py` passes;
  `types.ts` updated in the same change.
- **UI (vitest):** tool chip rendering from a scripted event sequence
  (call → spinner, result → resolved state, `is_error` → failure state);
  admin page DOM test following the existing `*.dom.spec.ts` pattern.
- **Validation:** full AGENTS.md check lists for both subprojects, real
  output quoted.

## Roadmap edits (same change)

`2026-06-10-roadmap-family-launch-design.md` gains:

- **M3.5 — per-call tool approval.** Run pauses on tool calls pending user
  approval in the chat UI, with a per-server `auto_approve` flag; slotted
  between M3 and M4.
- A follow-up note under M3: per-tool allowlist granularity, when a real
  case demands it.
