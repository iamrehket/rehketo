# M4: Agent worker split

**Date:** 2026-06-14
**Status:** Approved design.
**Roadmap:** M4 in `2026-06-10-roadmap-family-launch-design.md`. Builds directly on
M1's durable bus (`2026-06-10-durable-event-bus-design.md`) and cashes in the
"revisit at the M4 split" IOUs left across the run machinery.

## Problem

Run execution lives inside the API process: `POST /conversations/{id}/messages`
spawns `run_agent` as an `asyncio.Task` and registers it in a process-local
`RunTaskRegistry` (`messages.py:87`). Three consequences:

- An API deploy or restart kills every in-flight run; the startup sweep can only
  fail them (`sweep.py`). Runs cannot survive a deploy.
- The sweep assumes it owns *all* non-terminal runs, so a second process would
  force-fail runs alive in its siblings (`sweep.py:27`). Deployment is pinned to a
  single process.
- Tool execution (MCP clients, M3) runs inside the auth-holding API process —
  no isolation between the request surface and agent/tool execution.

M3 made runs long enough (tool calls, approval waits) that deploy-survival
matters; we have not deployed yet, so the structural change is cheapest now
(same pre-deploy logic as M1).

## Goals

- Run execution moves to a dedicated worker process. The API only inserts
  `queued` runs and serves streams; it never drives a graph.
- Multiple workers are *correct*, not just tolerated: a worker restart never
  touches runs alive in its siblings.
- Clean-boundary resume: a run that died at a checkpoint boundary
  (`queued` = never started; `pending_approval` = paused at an interrupt) is
  picked up and continued. This pays M3.5's durable-approval-resume IOU.
- A worker crash is detected and its orphaned `running` run is failed promptly,
  by any worker, without a process restart.
- The API kickoff path, the bus, the SSE wire format, and the UI contract are
  unchanged.

## Non-goals

- **Mid-stream resume.** A run that died mid-LLM-stream (`running`, owner gone)
  is *failed* (`error.code="process_restart"`), not resumed from its partial
  token stream. Resuming mid-turn would require reconciling already-published
  deltas against a re-emitted stream; deferred until a real need appears.
- **Global concurrency caps / backpressure.** M4 ships a per-worker concurrency
  limit only; excess runs wait in `queued`. A global ceiling and queue-depth
  signals wait for a second concrete case (charter rule 3).
- **A generic durable-jobs framework.** This is durable execution specialized to
  one job type (agent runs); `runs` is the queue. A reusable job runner earns its
  place when a *second* durable-job need appears (durable title generation, an
  email outbox, scheduled tasks) — not before.

## Design

### Process topology

`run_agent` is already nearly process-agnostic: it takes `(run_id, bus)`,
finalizes its own DB state, and owns its terminal events. The split moves *who
calls it* and *where the control listener and task registry live*, not its body.

- **API process:** keeps the event bus (for SSE subscribe + publishing approval
  decisions) and all HTTP handlers. **Loses** the `RunTaskRegistry` and
  `RunControlListener` — it no longer holds run tasks.
- **Worker process:** new entrypoint `python -m rehketo.cli.worker`. Owns the
  claim loop, a per-worker `RunTaskRegistry`, a `RunControlListener`, the reaper
  loop, and its own event bus instance for publishing run events. The worker loop
  is a plain reusable coroutine so the entrypoint is thin.

Both processes construct `PostgresEventBus` independently — it is already
per-process, and `event_bus.py:48` notes its process-local publish locks stay
correct after the split because all of a run's publishers live in the one worker
that owns it.

### The runs table as a claim queue

One migration: add `runs.heartbeat_at timestamptz` (nullable). `cancel_requested_at`
already exists (M1). No new status values — the existing enum
(`queued`/`running`/`pending_approval`/`succeeded`/`failed`/`cancelled`) is
sufficient. Add a partial index `runs(created_at) where status = 'queued'` for the
claim scan and `runs(heartbeat_at) where status = 'running'` for the reaper scan.

**Claim** (atomic, pool-safe):

```sql
UPDATE runs SET status='running',
                heartbeat_at=now(),
                started_at=COALESCE(started_at, now())
WHERE id = (SELECT id FROM runs
            WHERE status='queued'
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
RETURNING id, conversation_id, user_id, cancel_requested_at
```

`SKIP LOCKED` lets N workers claim disjoint runs without blocking each other.
Setting `heartbeat_at` in the *same* UPDATE that flips to `running` closes the
window where a just-claimed run could look stale to a reaper. `COALESCE` on
`started_at` preserves the original start across a resume.

**Doorbell + poll.** The API `NOTIFY`s a new `run_queued` channel on kickoff; each
worker `LISTEN`s and tries to claim on a wake. A periodic poll (~2s) is the floor,
so a missed NOTIFY costs latency, not a stuck run — the same doorbell philosophy
as the bus. A worker claims up to its per-worker concurrency limit (config, e.g.
4) and stops scanning until a slot frees.

**Cancel check at claim head.** Immediately after claiming, if
`cancel_requested_at` is set the worker finalizes the run as `cancelled` without
invoking the graph. This is how cancellation of a *parked* run resolves (see
below) and keeps all finalization in one place.

### State ownership model

| Status | Owner | On worker death | Recovery |
|---|---|---|---|
| `queued` | nobody | n/a (no owner) | claimed when a slot frees |
| `pending_approval` | nobody (parked) | n/a (no owner) | re-claimed when the decision flips it to `queued` |
| `running` | exactly one worker | orphaned row | reaper fails it (stale heartbeat) |

Only `running` has an owner that can die, so it is the only state needing liveness
detection.

### Liveness: heartbeat + reaper

While a run executes, the worker stamps `runs.heartbeat_at = now()` on a fixed
wall-clock timer (~15s), on an asyncio task *independent of stream progress* — a
single LLM turn can run 30–60s producing nothing streamable, and the heartbeat
asserts "the process owns this run," not "the agent is emitting output."

Each worker runs a reaper loop (~30s):

```sql
UPDATE runs SET status='failed',
                error='{"code":"process_restart",...}',
                finished_at=now()
WHERE status='running' AND heartbeat_at < now() - interval '60 seconds'
RETURNING id
```

The UPDATE is idempotent, so concurrent reapers across workers are safe — no
leader election. For each reaped id the reaper publishes the terminal event pair
(`run.status=failed`, `run.ended`) so an attached subscriber gets a clean close.
Threshold (60s) is a small multiple of the heartbeat interval (15s) to absorb a
missed beat or DB blip without false-failing a live run.

**This replaces the startup sweep.** `sweep_abandoned_runs` is deleted: orphaned
`running` rows are caught by the reaper (stale heartbeat), and `queued` /
`pending_approval` rows are claimable, not abandoned. The cold-start case (all
workers were down) is just the reaper's first pass finding stale heartbeats. This
retires the `sweep.py:27` single-process IOU.

### Durable approval resume (release-on-interrupt)

Today the worker that hits an approval interrupt blocks in `wait_for_decisions`
holding its slot (`approval.py:57`). That neither survives a restart (a blocked
worker's death strands the run in `pending_approval`, which the reaper does not
touch) nor frees the slot during an hours-long human wait. M4 inverts it:

1. Worker hits the interrupt, publishes one `tool.approval_required` per call
   (now including the interrupt id in the payload for correlation), sets
   `pending_approval`, and **exits the run loop, freeing its slot.** The paused
   graph lives in the LangGraph checkpoint (`thread_id=run_id`); no `run.ended`
   is published — the run is not terminal and the SSE stream stays open.
2. `POST /runs/{id}/approvals/{approval_id}` publishes the durable
   `tool.approval_decision` event as today, and additionally flips the run
   `pending_approval → queued` and `NOTIFY run_queued`.
3. A worker re-claims the run. LangGraph resumes from the checkpoint; the worker's
   `resolve_interrupt` finds the pending interrupt and the *already-published*
   decisions, builds the resume `Command`, and continues.

**Approval-id correlation across the boundary.** `approval.py:45` mints fresh
`uuid4` ids each pass; on a resume in a *different* worker, regenerating them would
orphan the published decisions. Fix: on encountering an interrupt, the worker
first queries `run_events` for existing `tool.approval_required` events for this
interrupt id. If present, it reuses those ids and looks for matching
`tool.approval_decision` events (resume path); if absent, it mints ids and
publishes (fresh path). `run_events` is already the durable source of truth, so
this is a query, not new storage. This also closes the `runs.py:190`
check-then-publish race: validation reads the journal, and re-claim is idempotent
because a second decision for a resolved id finds the interrupt already gone from
the checkpoint.

**Segment rehydration.** The approval boundary splits a single assistant turn:
pre-approval narration streamed as `message.delta` events but is persisted as
`Message` rows only at terminal finalization (`run.py:237`). The releasing
worker's in-memory `SegmentTracker` is lost. So on a resume (claimed run that
already has `run_events`), the worker rebuilds the `SegmentTracker` from the
persisted deltas before continuing the stream, so the final persisted message
spans the whole turn. Fresh runs (no prior events) start with an empty tracker as
today.

### Cancellation

The API `cancel_run` handler is unchanged in spirit: it calls `request_cancel`
(stamp `cancel_requested_at` + `NOTIFY run_control`). What changes is who reacts:

- **`running` run:** the owning worker's `RunControlListener` cancels the local
  task → `CancelledError` → the existing finalize-cancelled path in `run_agent`.
  The listener and registry now live in the worker. As a backstop against a lost
  NOTIFY (the `cancellation.py:41` gap), the heartbeat tick re-reads
  `cancel_requested_at` and cancels itself if set.
- **`queued` / `pending_approval` (parked) run:** no task to cancel.
  `request_cancel` additionally flips the run to `queued` and `NOTIFY run_queued`;
  a worker claims it, sees `cancel_requested_at` at the claim head, and finalizes
  `cancelled` without running the graph.

Routing parked-run cancellation through the claim keeps the invariant the codebase
already prizes — finalization is always the worker/orchestrator's job
(`run.py:139`), never split into an API handler.

### run_agent restructure

The body is preserved; the control flow around the resume loop changes:

- The resume loop no longer blocks on `wait_for_decisions`. Hitting an interrupt
  returns a non-terminal "parked" outcome that **skips the terminator** — the
  single-`run.ended`-in-`finally` invariant (`run.py:395`) becomes
  "`run.ended` on terminal outcomes only." Parked is not terminal.
- A claimed run carries a "resuming" flag (it has prior `run_events`); when set,
  the worker rehydrates the `SegmentTracker` and passes `Command(resume=...)`
  instead of the message input.
- `_set_status`'s unconditional write (`approval.py:110`) becomes safe under
  multi-worker because a parked run has exactly one claimer at a time (the
  `SKIP LOCKED` claim is the mutex), retiring that IOU.

### Dev ergonomics

API and worker stay genuinely separate processes (no dev/prod divergence, no
config knob). The justfile keeps per-process recipes and adds a combined
convenience recipe:

```just
# Run api + worker (dev convenience; prod runs them as separate services).
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT
    just api &
    just worker &
    wait
```

`trap 'kill 0' EXIT` tears down both on Ctrl-C. AGENTS.md's "one recipe per
process; run each in its own terminal" line is updated to bless `just dev` as the
combined convenience while keeping the per-process recipes as the canonical/prod
story.

## Error handling

- **Worker crash mid-run:** heartbeat stops; reaper fails the run within the
  threshold and publishes closure. No mid-stream resume (non-goal).
- **Reaper false-positive:** prevented by a threshold several heartbeat intervals
  wide; a genuinely slow turn keeps beating because the heartbeat is timer-driven,
  not output-driven.
- **Claim race:** `FOR UPDATE SKIP LOCKED` guarantees disjoint claims; no two
  workers run the same run.
- **Missed `run_queued` NOTIFY:** the per-worker poll picks the run up within the
  poll interval (latency, not loss).
- **Decision POST for an already-resumed run:** idempotent — the interrupt is gone
  from the checkpoint, so the resumed graph ignores a stale decision; the handler's
  journal check returns 409 on a duplicate.

## Testing

- **Claim concurrency:** N workers against M queued runs claim disjoint sets, none
  dropped, none doubled (real postgres, `SKIP LOCKED`).
- **Reaper:** a `running` row with a stale `heartbeat_at` is failed with
  `process_restart` and closure events reach a subscriber; a freshly-claimed row
  (recent heartbeat) is left alone; concurrent reapers don't double-finalize.
- **Heartbeat liveness:** a long-running stub run keeps its heartbeat fresh and is
  never reaped.
- **Durable approval resume:** park a run at `pending_approval`, drop the worker,
  POST the decision, a fresh worker re-claims and resumes; assert the final
  persisted message spans pre- and post-approval narration (segment rehydration);
  approval ids correlate across the boundary.
- **Parked-run cancel:** cancel a `queued` and a `pending_approval` run; a worker
  finalizes `cancelled` without running the graph.
- **Running-run cancel across processes:** cancel a run owned by a worker via the
  API process; the worker's control listener cancels it; the heartbeat-reread
  backstop catches a simulated lost NOTIFY.
- **Kickoff path:** `POST /messages` inserts `queued` + NOTIFYs and returns
  without spawning a task; no `run_agent` import remains in `messages.py`.
- **e2e (`-m e2e`):** the offline browser suite still passes end to end with
  execution behind the worker — run it (wire shapes/flows unchanged but the
  execution path moved).

## Validation

API: the full check block in `rehketo-api/AGENTS.md` (ruff format, ruff check,
mypy, bandit, lint-imports, pytest, `-m e2e`, contract check). Repo guards +
mirror check from root (AGENTS.md edit regenerates the mirrors). Manual: `just db`,
`just dev`; start a run and watch it stream; kill the worker mid-run and confirm
the reaper fails it cleanly on the next pass; trigger a tool approval, kill the
worker while parked, restart, approve, and watch the run resume and finish with
its pre-approval narration intact.
