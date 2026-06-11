# User preferences (M2): custom instructions + settings page

**Date:** 2026-06-10
**Status:** Approved design. Implements M2 of
`2026-06-10-roadmap-family-launch-design.md`.

## What and why

Per-user free-text custom instructions ("how I want the assistant to behave"),
edited on a new `/settings` page and injected into the agent's system prompt. The
injection happens through a new prompt-assembly function that replaces the
hardcoded `"You are a helpful assistant."` in `rehketo/agent/graph.py` — that
function is the seam compaction will later plug into (see roadmap, event-gated
items).

## Scope decisions

- **Theme preference is deferred.** The UI is dark-only today; light/dark mode
  requires designing a full light palette and becomes its own later item. The
  `user_preferences` table is where the theme column will land when it does.
- **Free text only.** No per-conversation overrides, no structured fields.
- **No admin view of preferences.** A user reads and writes only their own row.

## Schema (migration 0010)

New table `user_preferences`:

| column                | type        | notes                                          |
| --------------------- | ----------- | ---------------------------------------------- |
| `user_id`             | UUID        | PK, FK → `users.id` `ON DELETE CASCADE`        |
| `custom_instructions` | TEXT        | NOT NULL                                       |
| `updated_at`          | timestamptz | NOT NULL DEFAULT now(), set on every write     |

No row means "no preferences set." The row is created on first save (upsert) by
the API, never by the auth flow — `users` stays auth-owned, `user_preferences`
stays user-owned. This write-path separation is why a 1:1 table was chosen over
a column on `users` (and a JSONB blob was rejected as speculative flexibility,
charter rule 3).

## API (extends `rehketo/api/me.py`)

- `GET /me/preferences` → `{"custom_instructions": "<string>"}`. Returns `""`
  when no row exists; the UI never distinguishes "unset" from "empty."
- `PUT /me/preferences`, body `{"custom_instructions": "<string>"}` with Pydantic
  `max_length=4000`. Upserts the row, returns the stored value.

Both use the existing `resolve_permissions` dependency; the target row is always
the session user's, so there is no resource-level permission check. CSRF is
covered by the existing mutating-method middleware. The OpenAPI snapshot is
rebaselined and `tools/check_contract.py` must pass.

Error handling is boundary-only (charter rule 4): the length cap yields a 422
from Pydantic; DB errors bubble to the handlers in `rehketo/api/errors.py`.

## Prompt assembly (new `rehketo/agent/prompt.py`)

```python
def assemble_system_prompt(custom_instructions: str | None) -> str
```

- The base prompt (`"You are a helpful assistant."`) moves here from `graph.py`.
- Blank or `None` instructions → base prompt unchanged.
- Otherwise the instructions are appended under a delimited
  `## User instructions` section.

`rehketo/agent/run.py` fetches the user's preferences once at run start and
passes the assembled prompt into `build_agent`, which gains a `system_prompt`
parameter and loses its hardcoded string. Consequences:

- Edits apply from the next run onward, never mid-run.
- `build_agent` stays a pure function of its inputs — no hidden request-context
  coupling, which matters for the M4 worker split.

## UI

- **Route:** `src/routes/(app)/settings/+page.svelte` + `+page.ts`. The `(app)`
  group provides the authenticated layout and sidebar. The load function fetches
  `GET /me/preferences` via `apiFetch`.
- **Page:** one "Custom instructions" section — explainer line ("Included in
  every new chat"), textarea, live character counter against the 4,000 cap, Save
  button (disabled while unchanged or over-limit) that PUTs and reports via the
  existing toast store. Dark workbench styling; no new design language.
- **Entry point:** a "Settings" item in `UserMenu` above Logout, navigating via
  `goto` and closing the menu — same pattern as the logout item.
- **Types:** `Preferences` added to `src/lib/types.ts`. No new store; nothing
  else in the app reads preferences (injection is server-side).

## Testing

- **API (pytest):** GET with no row returns `""`; PUT creates then updates
  (both upsert paths); over-limit body → 422; unauthenticated → 401. Unit tests
  for `assemble_system_prompt` (None, blank, real text). One test asserting
  `run.py` passes the assembled prompt to `build_agent` when instructions are
  set.
- **Contract:** OpenAPI snapshot rebaselined; `check_contract.py` passes.
- **UI (vitest):** `settings.dom.spec.ts` following the existing
  `*.dom.spec.ts` pattern — renders the loaded value, counter updates, save
  calls `apiFetch` with the right body, failure shows a toast.
- **Validation:** full AGENTS.md check lists for both subprojects, real output
  quoted.
