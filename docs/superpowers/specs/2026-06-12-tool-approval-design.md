# Per-call tool approval (M3.5)

**Date:** 2026-06-12
**Status:** Approved design. Implements M3.5 of
`2026-06-10-roadmap-family-launch-design.md`.

## What and why

A run pauses when the agent wants to call a tool from an untrusted MCP
server; the user approves or denies the call from the chat UI; the run
resumes. A per-server `auto_approve` flag preserves M3's auto-execute
behavior for servers the admin trusts. M3 made the admin allowlist the only
safety boundary; M3.5 adds the per-call human gate for everything inside
that boundary.

## Scope decisions

- **In-process wait; restart abandons the run.** The run task stays alive,
  waiting for the decision. An API restart sweeps a pending-approval run as
  failed — exactly what happens to every other in-flight run today. Durable
  resume across restarts is M4's problem (resumption reconciliation); the
  LangGraph checkpoint written at the interrupt is the seam M4 will use.
- **Deny feeds the model, never aborts.** A denied call returns a rejection
  message to the model as the tool result; the run continues, so the agent
  can explain or try another approach. "Stop everything" is what the
  existing cancel button is for.
- **Decisions are approve and deny only.** The middleware also supports
  editing arguments and free-text responses; both wait for a real case
  (charter rule 3).
- **`auto_approve` defaults to false for all servers**, including existing
  rows in the migration backfill. Approval-required is the safe default and
  one uniform rule; admins flip the flag on servers they trust.
- **No approval timeout.** A pending run waits until decided, cancelled, or
  swept by a restart.
- **Out of scope:** per-tool approval granularity (the flag is per-server,
  like the allowlist), remembered decisions ("always allow this tool"),
  approval delegation to other users.

## Schema (migration 0012)

- `mcp_servers.auto_approve BOOLEAN NOT NULL DEFAULT false`; existing rows
  backfill to `false`.
- The `runs_status_enum` check constraint gains `'pending_approval'`:
  `('queued','running','pending_approval','succeeded','failed','cancelled')`.

`auto_approve` joins the admin API request/response models (create, PATCH,
list). The OpenAPI snapshot is rebaselined and `tools/check_contract.py`
must pass.

## Interrupt wiring

deepagents' built-in HITL (`create_deep_agent(interrupt_on=...)`, backed by
`HumanInTheLoopMiddleware`) pauses the graph **before** the tool executes —
so the adapter's `tool.call` / `tool.result` publishing is untouched and
cannot double-fire across a resume.

- `rehketo/mcp/registry.py` — `build_run_toolset` already knows each tool's
  server; it additionally returns an `interrupt_on` dict mapping each tool
  from an `auto_approve=false` server to
  `InterruptOnConfig(allowed_decisions=["approve", "reject"])`. Tools from
  trusted servers are absent from the dict (unlisted tools auto-approve).
- `rehketo/agent/graph.py` — `build_agent` gains an `interrupt_on=`
  parameter passed straight to `create_deep_agent`, staying a pure function
  of its inputs.

## The resume loop (`rehketo/agent/run.py`)

The single `astream` call becomes a loop in the same asyncio task:

1. Stream until the graph stops, transforming chunks as today.
2. Read the checkpoint state for pending interrupts. None → break to
   finalization as today.
3. Publish one `tool.approval_required` event per interrupted call
   (approval id, tool name, arguments); set the run to `pending_approval`
   (DB update + `run.status` event).
4. Await decisions for the whole batch — LangGraph emits parallel tool
   calls as one interrupt batch, and the graph resumes only when every call
   is decided.
5. Set the run back to `running` (DB + event), re-invoke
   `astream(Command(resume=<decisions>))`, continue the loop. The
   middleware executes approved calls (adapter events flow as in M3) and
   synthesizes a rejection `ToolMessage` for denied ones.

Cancel-while-pending needs no new code: the control listener cancels the
task, `CancelledError` propagates out of the decision wait, and the
existing cancel branch finalizes. `TERMINAL_RUN_STATES` is unchanged —
`pending_approval` is non-terminal, so the cancel endpoint accepts it.

The startup sweep (`rehketo/agent/sweep.py`) adds `pending_approval` to its
swept states; an abandoned pending run fails on restart like any other.

## Decision transport: durable events on the existing bus

New endpoint in `rehketo/api/runs.py`:

```
POST /runs/{run_id}/approvals/{approval_id}   {"decision": "approve" | "deny"}  → 204
```

Guards, in order: new action `chat.approve_tool_call` (added to `ACTIONS`,
granted to every role with `chat.write` — the M3 precedent), run owned by
the caller (404 otherwise), run status is `pending_approval` (409
otherwise), and `approval_id` matches a pending approval — an
`approval_required` event in `run_events` without a matching decision event
(409 on duplicates or unknown ids). CSRF is covered by the existing
mutating-method middleware.

The endpoint publishes `tool.approval_decision` to the durable bus. The
waiting run task subscribes to its own run's event stream and collects
decision events until the batch resolves. No new table, no new NOTIFY
channel; the decision is durably journaled — transcript reload and audit
for free — and the transport is multi-process-correct the same way the
event bus already is.

## SSE events

Two new types in the stable event schema, flowing through the durable bus
(persisted to `run_events`, resumable via `from_sequence`):

```json
{"type": "tool.approval_required", "run_id": "...", "sequence": 9,
 "approval_id": "...", "tool": "github__create_issue", "arguments": {"title": "..."}}
{"type": "tool.approval_decision", "run_id": "...", "sequence": 10,
 "approval_id": "...", "decision": "deny"}
```

`run.status` events gain `"pending_approval"` as a value.

## UI

- **Types:** both events join the `RunEvent` union in `src/lib/types.ts`;
  run status handling gains `pending_approval`.
- **Stream handling:** `subscribeRun` in `src/lib/sse.ts` gains
  `onApprovalRequired` / `onApprovalDecision` handlers.
- **Approval card:** rendered chronologically in the transcript when
  `approval_required` arrives — tool name, expandable arguments JSON (same
  treatment as the tool chip), Approve and Deny buttons posting to the
  endpoint. The card resolves on the `approval_decision` **event**, not the
  POST response, so a second tab resolves too. Approved → the normal tool
  chip follows; denied → the card shows a denied state. The running
  indicator reads "waiting for approval" while status is
  `pending_approval`.
- **Reload:** transcript reconstruction already interleaves `run_events`;
  approval events join it. An undecided approval on a `pending_approval`
  run renders the live card with buttons; decided ones render resolved.
- **Admin page:** `auto_approve` toggle in the create form and per-row,
  following the `enabled` toggle pattern.

## Error handling

Boundary-only (charter rule 4):

- Endpoint: 404 unknown/unowned run, 409 not-pending or already-decided.
  First decision wins; the run task ignores decision events for unknown or
  already-resolved approval ids.
- The middleware's resume payload format is the one external contract we
  don't own — pinned by an integration test against the real API, not by
  defensive code.
- Tool failures after approval behave exactly as M3 (`is_error` result back
  to the model).

## Testing

- **Unit:** registry builds `interrupt_on` correctly from `auto_approve`
  (mixed trusted/untrusted servers, all-trusted → empty dict);
  decision-wait helper (batch completion, ignores unknown ids); event
  shapes.
- **Integration (pytest, real postgres, in-process fastmcp server through
  the client seam):** full cycle — run pauses with status
  `pending_approval` and an `approval_required` event; approve → tool
  executes, `tool.call`/`tool.result` flow, run succeeds; deny → model
  receives the rejection and the run completes with assistant text;
  cancel-while-pending → cancelled cleanly; endpoint guards (wrong user,
  not pending, duplicate decision, unknown approval id); sweep fails a
  pending run.
- **Contract:** OpenAPI snapshot rebaselined; `check_contract.py` passes;
  `types.ts` updated in the same change.
- **UI (vitest):** approval card state transitions from a scripted event
  sequence (required → buttons, decision event → resolved, denied state);
  reload rendering of an undecided approval; admin `auto_approve` toggle
  DOM test.
- **Validation:** full AGENTS.md check lists for both subprojects, real
  output quoted.

## Roadmap edits (same change)

`2026-06-10-roadmap-family-launch-design.md`: M3.5 gains a spec reference
(`2026-06-12-tool-approval-design.md`).
