# Roadmap: from working chat to family launch

**Date:** 2026-06-10 (revised 2026-06-11: M1–M3 shipped; added M5–M7 and M9–M12;
promoted OpenFGA from event-gated to sequenced; renumbered family onboarding → M8
and MCP apps → M13)
**Status:** Approved direction. Each milestone gets its own spec → plan → implementation
cycle; this document only fixes ordering, scope boundaries, and triggers.

## Where we are

Chat works end to end: Entra sign-in, conversations, agent runs (deepagents +
LangGraph) streamed over SSE, one Bifrost model alias behind the `AGENT_MODEL` seam.
M1 (durable event bus), M2 (user preferences), and M3 (MCP tool calling) are
implemented. Two user types now exist — standard users and admins — gated by
roles through the permissions gate. A design system (built via Claude design)
exists but is not yet applied to the UI. The only real user is the maintainer.
Family is the first user group; friends come later.

This roadmap re-prioritizes the fast-follow list in
`rehketo-api/docs/superpowers/specs/2026-04-19-chat-and-agent-v1-design.md` and adds
two new items (user preferences, compaction). Where the two documents disagree on
ordering, this one wins.

## Sequenced milestones

### M1 — Durable event bus + stream resumption + cross-process cancellation — shipped

Replace the in-process event bus with postgres LISTEN/NOTIFY, persisting events to
the existing `run_events` table; add SSE resume-from-sequence on reconnect; move
cancellation to a durable column + NOTIFY so it works across processes and the
deployment is multi-worker safe. Today an API restart or deploy kills any
in-flight stream with no recovery.

Scope decision: cross-process cancellation was pulled into M1 (rather than
deferred to the worker split) because we have not deployed yet — structural
changes are cheapest now, and deferring means re-touching the same code after
deploy. Spec: `2026-06-10-durable-event-bus-design.md`.

Why first: reliability is the family's first impression, and every later feature —
tool events especially — rides on this transport.

### M2 — User preferences — shipped

Per-user free-text preferences (Claude-style "how I want the assistant to behave"),
stored per user, edited on a settings page, injected into the system prompt by a new
prompt-assembly function that replaces the hardcoded string in
`rehketo/agent/graph.py`.

Scope decisions made: free text only — no per-conversation overrides, no structured
fields. The prompt-assembly function is the seam compaction will later use.

### M3 — MCP tool calling (host role) — shipped

Rehketo acts as an MCP **host**: a generic MCP client connects to admin-configured
external servers. The server list is config/data, never code (north star). Tools
flow into the existing `tools=[]` seam in `rehketo/agent/graph.py` through a tool
registry; tool-call and tool-result events stream over SSE and render in the
transcript.

Includes a deliberately simple per-role tool allowlist enforced through the existing
permissions gate (`rehketo/permissions/check.py`). "Who can invoke what" cannot wait
for OpenFGA once tools exist.

Out of scope: MCP apps (M13), user-authored servers, per-user server configuration.
fastmcp may serve as the client library and, later, as the framework for any
built-in tools we author — those would register through the same registry, not a
parallel path. Spec: `2026-06-11-mcp-tool-calling-design.md`.

### M3.5 — Per-call tool approval

Run pauses on a tool call pending user approval in the chat UI, with a
per-server `auto_approve` flag so trusted servers keep M3's auto-execute
behavior. Pulls in LangGraph interrupt/resume, a pending-approval run state,
new SSE events, and approval UI — deliberately split out of M3 so plain tool
calling ships first.

Also queued from M3: per-tool allowlist granularity (M3 gates per server;
per-tool waits for a real case that demands it).
Spec: `2026-06-12-tool-approval-design.md`.

### M4 — Agent worker split

Move run execution out of the API into a dedicated worker process. The API only
inserts `runs` rows (`status='queued'`) and serves streams; the worker claims
queued runs (`SELECT ... FOR UPDATE SKIP LOCKED` + NOTIFY doorbell), executes the
LangGraph graph, and publishes to the durable bus. API and worker share no memory
— the `runs` table is the queue, `run_events` is the stream, the M1 cancel channel
works unchanged.

What it buys: runs survive API deploys entirely; worker restarts can resume runs
from LangGraph checkpoints (`thread_id=run_id`) instead of failing them; agent and
tool execution is isolated from the auth-holding API process — which matters once
M3 puts tool calls inside runs; `queued` gains real semantics (concurrency caps,
backpressure).

The hard design problem to solve in its spec: resumption reconciliation — what
happens to a partially-streamed assistant message and already-published events
when a run resumes from a checkpoint.

Why here: M1 builds the seam it stands on; M3 makes runs long enough (tool calls)
for deploy-survival to matter; doing it before family onboarding lands the
structural change before real users — the same pre-deploy logic as M1's scope.

### M5 — Design refresh

Apply the existing design system (built via Claude design) to `rehketo-ui`. The
system is already created; this milestone is adoption, not design work from
scratch.

Why here: the roadmap's own logic says reliability is the family's first
impression — visual polish is literally the first impression. It is also cheapest
now: M3.5's approval UI and later milestones (file uploads, MCP apps) all add UI
surface, and restyling grows more expensive with each one.

### M6 — Session elevation

The v1 auth spec committed to a second `session_elevated` cookie (`SameSite=Strict`,
short TTL, granted via a re-authentication step) "as part of the same increment
that introduces the first dangerous action." That increment has happened: admin
operations exist (MCP server administration is permission management). This
milestone pays the committed debt: the elevated cookie, the FastAPI dependency
analogous to `resolve_permissions`, and elevation requirements on admin endpoints.
Elevation is orthogonal to permissions — both gates must pass.

### M7 — Deployment & ops

First real deployment: domain + TLS, secrets handling, production database
migration story, **backups** (the database is about to hold other people's
conversations), basic monitoring/alerting, and Bifrost production config. The
deployment shape (compose: postgres, Bifrost, API serving the built UI) is
already specified in the v1 design; this milestone makes it real. Mostly ops,
not code.

Why here: M8 assumes a running instance family can reach; until now deployment
was implied between milestones but owned by none of them.

### M8 — Family onboarding

Invite family into the Entra tenant (guest or member accounts) and onboard them.
Mostly ops, not code; budget for small UX fixes that surface from real first-time
users.

Why after M3: there is no reason to use Rehketo until tools make it useful.
Decision made: family signs in with Entra. Fast-tracking Google sign-in was
considered and rejected — weeks of auth work versus an afternoon of tenant admin,
and multi-IdP's real design driver (account linking) arrives with friends, not
family.

### M9 — File uploads & multimodal input

Upload files and images in chat: an upload endpoint and storage (permission-
checked), composer attach + transcript rendering in the UI, and multimodal
message content through the `AGENT_MODEL` seam so the model can see images.

This is **host-side** work; an onboarded MCP server cannot provide it. MCP has
no client→server file-upload primitive — tool-call arguments are plain JSON, and
the model emits them, so raw file bytes can never ride in arguments. The pattern:
the model passes an opaque file reference (`file_id`) in tool args, and the tool
registry's execution layer resolves the reference — injecting base64 content or
an authenticated URL — before invoking the MCP server. That resolution shim is
ours, in the same registry M3 built; servers consume files, they don't receive
uploads.

### M10 — Token accounting & usage visibility

Per-user usage (tokens, cost) surfaced to admins. Bifrost's governance layer
already meters traffic, so this may be more surfacing than building. It is also
the precondition for compaction (we cannot compact what we cannot measure) and
the seam for any future per-user caps.

Why after M8: there is nothing to account for until family generates usage.

### M11 — OpenFGA

The trigger fired: two genuinely different user types exist (standard vs. admin,
expressed today as `allowed_roles` on MCP servers). Swap the body of the
permissions gate for OpenFGA; call sites already go through the gate interface,
so this is a body-swap, not a call-site migration.

Why this late: the current gate works and the change is user-invisible. Letting
real multi-user usage accumulate first means the relationship model is designed
against observed needs, not guessed ones.

### M12 — OAuth connections

Exercise the scaffolded `connections` table and consent route pair (north star).
The concrete driver: the first MCP server that must act **as the user** against
a downstream service (Google / GitHub / MS Graph) rather than with a server-held
bearer token. Every provider follows the one pattern — consent route pair plus a
`connections` row with provider + scopes.

### M13 — MCP apps

Interactive UI widgets rendered in chat: sandboxed iframe + postMessage bridge in
the SvelteKit UI. Security-sensitive and large; gets its own design pass.

Gated on M3 being in real use. The MCP apps spec is young; letting it mature while
plain tool calling proves out is deliberate.

## Event-gated milestones

These have no sequence position. Each starts when its trigger fires, and is spec'd
then — not now.

### Multi-IdP — trigger: friends arriving

Google first, GitHub second, Discord last (plain OAuth2, no `id_token` — the odd
one out relative to the OIDC providers). The `identities` table already supports
multiple providers per user; the real design work is the account-linking flow and
email-merge policy.

### Compaction — trigger: a real conversation degrades

Token accounting (M10) comes first — we cannot compact what we cannot measure.
The shape of compaction itself (summarize-and-truncate or otherwise) is
intentionally unspecified until the trigger fires. It will plug into the
prompt-assembly seam created in M2.

(OpenFGA formerly lived here. Its trigger — a second user *type* genuinely
exists — fired with the standard/admin split, so it moved into the sequence
as M11.)

## Standing concerns

- **Tool authorization precedes OpenFGA.** Handled inside M3 via the existing gate;
  do not let it grow into a parallel authorization system.
- **Discord is not OIDC.** Budget extra time for it within multi-IdP; do not let
  its quirks leak into the shared identity flow.
- **MCP apps spec maturity.** Re-check the spec's state when M13 approaches;
  scope to what the spec firmly supports.
