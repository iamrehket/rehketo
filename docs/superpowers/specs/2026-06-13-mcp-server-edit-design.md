# MCP server edit (UI)

**Date:** 2026-06-13
**Status:** Approved design. Closes the UI gap left by
`2026-06-11-mcp-tool-calling-design.md` and `2026-06-12-tool-approval-design.md`.

## What and why

An admin can edit an existing MCP server's `url`, `auth_token`,
`allowed_roles`, and `auto_approve` in place. Today the settings page only
exposes create, delete, and the two toggles (`enabled`, `auto_approve`), so
changing a URL, rotating a token, or adjusting roles means delete-and-recreate
— losing the row's identity and history. The backend already supports the full
edit: `PATCH /admin/mcp-servers/{id}` with `McpServerPatch`
(`rehketo-api/rehketo/api/mcp_servers.py:155`). This is a **UI-only** change;
no API, schema, or migration work.

## Scope decisions

- **UI only.** The PATCH endpoint, partial-update semantics, token encryption,
  role validation, and permission checks all exist and are tested. We add no
  backend code.
- **`name` stays immutable.** It is the tool prefix (`{name}__{tool}`) and the
  unique key; the API rejects changing it by omitting it from `McpServerPatch`.
  The edit form shows it read-only.
- **Inline expand, one row at a time.** Editing happens in place in the list
  row, not a modal (no modal infrastructure exists in the UI — adding one is
  out of scope) and not by repurposing the bottom form (which would jump focus
  away from the row). Opening a second editor closes the first.
- **Token: keep / replace / clear.** The token is write-only — the API returns
  only `has_auth_token`, never the value — so the edit field always opens
  blank. The three intents the backend already distinguishes are all exposed
  (see below). Exposing only keep/replace would leave the clear capability
  unreachable from the UI (an orphan, charter rule 8).
- **Out of scope:** editing `name` (recreate instead), bulk edit, optimistic
  concurrency / edit conflict detection (single-admin assumption holds as it
  does for every other admin mutation today), any new modal/dialog component.

## Component shape

Extract the existing inline create-form markup into a reusable
`rehketo-ui/src/lib/components/McpServerForm.svelte`. This is the second
concrete case for the field markup (create + edit), which is the bar for
factoring it out (charter rule 3); it also avoids duplicating ~40 lines of
fields and their validation (charter rule 2, edit > create).

- **Props:** `server: McpServerOut | null` (null = create mode) and `busy:
  boolean`.
- **Output:** emits a `submit` event carrying a typed payload object. The form
  owns field state and the keep/replace/clear logic; the **parent**
  (`settings/mcp-servers/+page.svelte`) decides `POST` (create) vs
  `PATCH /{id}` (edit) and reconciles the `servers` list. The form never calls
  the API itself — keeping the network decision with the list that owns the
  data.
- **Create mode** (`server === null`): `name` editable, all roles
  default-checked, submit label "Add". Matches today's behavior exactly.
- **Edit mode** (`server` set): fields pre-filled from the server, `name`
  rendered read-only, submit label "Save", plus a "Cancel" that collapses the
  row with no change.

## Token field behavior

When editing a server with `has_auth_token === true`:

- Placeholder reads "leave blank to keep current token".
- Blank field, "Remove existing token" unchecked → **omit** `auth_token` from
  the PATCH body → keep current. (Relies on `model_fields_set`: an absent key
  means unchanged.)
- A typed value → `auth_token: "<value>"` → replace.
- "Remove existing token" checkbox (rendered only when `has_auth_token`),
  checked → `auth_token: null` → clear. Typing a value overrides/disables the
  checkbox so the two intents can't conflict.

In create mode the token field is unchanged from today (optional, write-only,
no remove checkbox).

## Interaction

- Each list row gains an **Edit** button alongside the existing
  Enable/Disable, auto-approve, and Delete buttons.
- Clicking Edit expands that row to render `McpServerForm` in edit mode for
  that server. A module-level `editingId` tracks which row is open; opening
  another row reassigns it, so only one editor shows at once.
- Save: on the parent's PATCH success, replace the row in `servers` and
  collapse. Cancel: collapse, no request. Failures reuse the existing `fail()`
  helper + toast pattern.

## Error handling

No new error paths. The API already returns 404 (gone), 409 (name conflict —
create only), 403 (forbidden, handled by the global hook), and 422
(validation). The existing `fail(action, err)` helper and toast pattern cover
all of them; edit reuses it with the `'update'` action label already used by
the toggles.

## Testing

- **TDD the payload mapping first.** The keep/replace/clear → PATCH-body
  construction is the one piece of real logic and the one place a wrong body
  silently corrupts state. Pin it with a unit test over a pure
  `buildPatchPayload(formState, server)` helper before wiring the form.
- **Component/DOM test** (`McpServerForm` and the page): create mode submits
  the same body as today; edit mode pre-fills and renders `name` read-only;
  the three token intents produce the correct bodies; one editor open at a
  time.
- **Run the e2e flow** (`uv run pytest -m e2e` from `rehketo-api/`, needs
  postgres up). AGENTS.md flags this suite as opt-in and prone to silent rot
  when wire shapes or UI flows change — this changes a UI flow, so run it.

## Validation block

From `rehketo-ui/`:

```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

From `rehketo-api/` (UI flow touched):

```bash
uv run pytest -m e2e
```

No `check_contract.py` rebaseline: the OpenAPI surface is unchanged.
