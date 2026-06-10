# M1: Durable event bus, stream resumption, cross-process cancellation

**Date:** 2026-06-10
**Status:** Approved design.
**Roadmap:** M1 in `2026-06-10-roadmap-family-launch-design.md`. Expands on the
`PostgresEventBus` sketch in §6.2 of the chat-and-agent v1 design.

## Problem

The event bus (`rehketo/runs/event_bus.py`) is in-memory and single-process. An API
restart or deploy kills every in-flight stream with no recovery, and a page reload
loses the live run. Run cancellation works only in the process that started the run
(`RunTaskRegistry`). We have not deployed yet — this is the cheapest moment for the
structural fix.

## Goals

- Events survive process restarts; any process can serve any run's stream.
- Clients reconnect and resume from the last sequence they saw.
- Cancellation works regardless of which process owns the run — safe to run
  multiple uvicorn workers.
- A client subscribed to a run that died with its process gets a clean close, not
  a hang.

## Non-goals

- Dedicated agent worker process and run-execution resumption (own milestone,
  after M3 — see roadmap).
- Event retention/pruning. Family-scale growth is trivial; prune when it hurts.
- UI contract changes. The SSE wire format and `from_sequence` semantics are
  already what we need.

## Design

### PostgresEventBus

New implementation of the existing `RunEventBus` protocol, replacing
`InProcessEventBus` at the single DI site (`main.py`).

**publish(run_id, event):** INSERT into `run_events` (table already exists,
unique on `(run_id, sequence)`), then `NOTIFY` on one shared channel with the
run_id as payload. Sequence comes from a per-run counter held by the publisher —
safe because each run has exactly one publisher, its `run_agent` task; the counter
initializes from `max(sequence)` for the run (0 for new runs).

**subscribe(run_id, from_sequence):** initial SELECT of existing rows from
`from_sequence` onward (ordered by sequence), yield them, then wait for wakes and
read any rows newer than the last yielded sequence.

**The doorbell pattern.** NOTIFY never carries event payloads — postgres caps
payloads at ~8KB and deltas can exceed it. NOTIFY says only "run X has news";
subscribers read the table. The table is the source of truth; NOTIFY is an
unreliable optimization over a reliable substrate. A periodic re-poll (~5s) per
subscriber catches any missed notification.

**One LISTEN connection per process,** owned by app lifespan, dispatching wakes to
per-subscriber asyncio queues by run_id. Not one connection per subscriber —
LISTEN holds a dedicated connection, and per-subscriber connections would exhaust
the pool. The listener task reconnects with backoff if its connection drops;
subscribers fall back to the re-poll interval meanwhile.

Replay of finished runs works with no special casing: subscribe reads all rows
including the final `run.ended`, and the SSE handler closes on it as today.

### Cross-process cancellation

`POST /runs/{id}/cancel` currently calls the in-process registry directly. It
becomes: set `runs.cancel_requested_at = now()` (new column, one migration — the
durable record), then NOTIFY a control channel with the run_id. Every process
listens on the control channel; the one whose `RunTaskRegistry` holds the run
cancels the local task. Processes without the run ignore the notification. The
registry survives as a process-internal detail.

If no process owns the run (it died in a restart), the startup sweep has already
failed it or will; the 409-on-terminal check in the handler is unchanged.

### Startup sweep publishes closure

The existing sweep marks orphaned `running` runs as `failed`
(`error.code="process_restart"`). It additionally publishes `run.status=failed`
and `run.ended` through the bus, so clients attached to a dead run's stream get
the normal terminal sequence instead of hanging. With a durable bus this is just
two `publish` calls.

### UI: reconnect and resume

`sse.ts` already accepts `fromSequence`; nothing upstream uses it. Two additions
in the UI:

1. **Mid-run reconnect.** `ChatView.svelte` tracks the highest sequence seen. On a
   connection error before a terminal state, it resubscribes with
   `from_sequence = last + 1` — capped exponential backoff, small attempt limit,
   then the existing error surface. `from_sequence` is exclusive of what was seen,
   so no duplicate deltas to dedupe.
2. **Resume on open.** Opening a conversation whose latest run is `queued` or
   `running` subscribes from sequence 0 and replays the stream. This is what makes
   durability visible: start a run on the phone, watch it finish on the laptop.

### Cutover and cleanup

`InProcessEventBus` is deleted (charter rule 8). During development the bus
contract tests run parametrized against both implementations; at cutover the
in-process rows of the parametrization go away with the class.

## Error handling

- Listener connection loss: reconnect with backoff; re-poll keeps streams alive
  (degraded latency, no data loss).
- Publish failure (DB down): the run fails — same blast radius as any other DB
  write in `run_agent`; no new handling.
- Missed NOTIFY: covered by re-poll. Sequence ordering comes from the table, so
  late wakes can't reorder events.

## Testing

- **Bus contract suite** (parametrized over implementations until cutover):
  publish/subscribe ordering, `from_sequence` replay, late subscribe to a finished
  run, concurrent subscribers.
- **Cross-connection integration:** publish via one connection, receive via a
  subscriber wired to another — proves NOTIFY wiring, not just table reads.
- **Restart simulation:** publish, drop the bus instance, build a fresh one,
  subscribe from 0 — full replay; sweep test asserts closure events reach a
  subscriber.
- **Cancellation:** cancel a run via the control channel from a bus instance that
  does not hold the task registry entry.
- **UI:** `sse.spec.ts` gains reconnect cases against the mock `EventSource` —
  resume sequence math, backoff cap, no-duplicate-delta on resume; ChatView test
  for subscribe-on-open of an active run.

## Validation

API: the full check block in `rehketo-api/AGENTS.md` (ruff, mypy, bandit,
lint-imports, pytest, contract check). UI: lint, check, `test:unit -- --run`.
Manual: start a run, kill the API mid-stream, restart, reload — the conversation
shows the swept failure cleanly; start a run in one tab, open the conversation in
a second tab and see the live stream.
