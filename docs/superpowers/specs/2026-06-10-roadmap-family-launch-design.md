# Roadmap: from working chat to family launch

**Date:** 2026-06-10
**Status:** Approved direction. Each milestone gets its own spec → plan → implementation
cycle; this document only fixes ordering, scope boundaries, and triggers.

## Where we are

Chat works end to end: Entra sign-in, conversations, agent runs (deepagents +
LangGraph) streamed over SSE, one Bifrost model alias behind the `AGENT_MODEL` seam.
The only real user is the maintainer. Family is the first user group; friends come
later.

This roadmap re-prioritizes the fast-follow list in
`rehketo-api/docs/superpowers/specs/2026-04-19-chat-and-agent-v1-design.md` and adds
two new items (user preferences, compaction). Where the two documents disagree on
ordering, this one wins.

## Sequenced milestones

### M1 — Durable event bus + stream resumption

Replace the in-process event bus with postgres LISTEN/NOTIFY, persisting events to
the existing `run_events` table, and add SSE resume-from-sequence on reconnect.
Today an API restart or deploy kills any in-flight stream with no recovery.

Why first: reliability is the family's first impression, and every later feature —
tool events especially — rides on this transport.

### M2 — User preferences

Per-user free-text preferences (Claude-style "how I want the assistant to behave"),
stored per user, edited on a settings page, injected into the system prompt by a new
prompt-assembly function that replaces the hardcoded string in
`rehketo/agent/graph.py`.

Scope decisions made: free text only — no per-conversation overrides, no structured
fields. The prompt-assembly function is the seam compaction will later use.

### M3 — MCP tool calling (host role)

Rehketo acts as an MCP **host**: a generic MCP client connects to admin-configured
external servers. The server list is config/data, never code (north star). Tools
flow into the existing `tools=[]` seam in `rehketo/agent/graph.py` through a tool
registry; tool-call and tool-result events stream over SSE and render in the
transcript.

Includes a deliberately simple per-role tool allowlist enforced through the existing
permissions gate (`rehketo/permissions/check.py`). "Who can invoke what" cannot wait
for OpenFGA once tools exist.

Out of scope: MCP apps (M5), user-authored servers, per-user server configuration.
fastmcp may serve as the client library and, later, as the framework for any
built-in tools we author — those would register through the same registry, not a
parallel path.

### M4 — Family onboarding

Invite family into the Entra tenant (guest or member accounts) and onboard them.
Mostly ops, not code; budget for small UX fixes that surface from real first-time
users.

Why after M3: there is no reason to use Rehketo until tools make it useful.
Decision made: family signs in with Entra. Fast-tracking Google sign-in was
considered and rejected — weeks of auth work versus an afternoon of tenant admin,
and multi-IdP's real design driver (account linking) arrives with friends, not
family.

### M5 — MCP apps

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

Token accounting comes first — we cannot compact what we cannot measure. The shape
of compaction itself (summarize-and-truncate or otherwise) is intentionally
unspecified until the trigger fires. It will plug into the prompt-assembly seam
created in M2.

### OpenFGA — trigger: a second user *type* genuinely exists

Swap the body of the permissions gate; call sites are already written against the
gate interface, so deferral is free. Multiple identity providers alone do not
trigger this — only genuinely different authorization needs do.

## Standing concerns

- **Tool authorization precedes OpenFGA.** Handled inside M3 via the existing gate;
  do not let it grow into a parallel authorization system.
- **Discord is not OIDC.** Budget extra time for it within multi-IdP; do not let
  its quirks leak into the shared identity flow.
- **MCP apps spec maturity.** Re-check the spec's state when M5 opens; scope to
  what the spec firmly supports.
