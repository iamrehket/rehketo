# Skills visibility & management in Settings (M4.5 follow-on)

**Date:** 2026-06-16
**Status:** Approved design. Continues
`2026-06-15-skills-progressive-discovery-design.md` (M4.5). Builds the surfaces
that spec deferred: admin CRUD, user authoring, and a user-facing view of the
skills available to a run. A holistic **design refresh** of Settings follows
this work as a separate phase and is out of scope here.

## What and why

M4.5 Slice 1 shipped the skills *engine* — a `skills` table, `resolve_skills`,
doc-skill materialization, mcp-skill subagents, all wired into agent runs — but
no HTTP surface. Skills exist only as data the run loop consumes; they are
populated by direct DB insert. The consequence: **a user has no window into
which skills the agent can draw on for them**, and there is no supported way to
create or edit a skill.

This spec closes that gap with three surfaces, all reached from Settings:

1. **Visibility** — every user can see the skills the agent can use for them
   (their resolved global + owned set).
2. **User authoring** — a user can create and edit their own **doc**-skills.
3. **Admin management** — an admin can CRUD all skills, global doc and global
   mcp alike, mirroring the existing `/settings/mcp-servers` admin surface.

## Scope decisions

- **Full close-out of the deferred slices, in one spec, built in slices.** The
  implementation plan orders the work so the core "visibility" fix lands first
  and independently (see §Slicing).
- **User authoring is doc-skills only this phase.** A doc-skill is
  self-contained markdown (trigger + instructions) with a clean trust
  boundary. User-authored **mcp**-skills — a user wrapping an
  already-onboarded MCP server they're allowed to use into their own subagent
  — are the long-term goal but a **later phase**; they pull in subagent /
  tool-approval / server-allowlist machinery not built here.
- **Admins author both kinds.** Admin CRUD covers global doc-skills and global
  mcp-skills (the latter binding an existing MCP server).
- **One role-aware page.** All three surfaces live on a single
  `/settings/skills` route that adapts to the caller's capabilities, rather
  than splitting into separate user/admin routes. Everything about skills is
  in one place.
- **Owned shadows global.** Skill names are namespaced per owner; a user's own
  skill may reuse a global skill's name and wins in that user's runs (a
  deliberate override). This is a structural schema change, not a patch (no end
  users yet — favor the structural fix).
- **Self-authoring is gated by a capability, not just authentication.** A new
  `chat.author_skill` action lets a deployment turn authoring off per-role and
  threads through the same permission seam as everything else.
- **Functional styling only.** The page reuses existing Tailwind tokens and the
  MCP-server form patterns. Visual polish is the separate design-refresh phase
  that follows.

## Data model

Migration `0015`. No new columns — `name`, `display_name`, `trigger`,
`instructions`, `allowed_roles`, `enabled`, `owner_user_id`, `mcp_server_id`
all exist from `0014`. The only change is **uniqueness**: drop the global
`UNIQUE(name)` and replace it with two partial unique indexes —

- `UNIQUE(name) WHERE owner_user_id IS NULL` — globals unique among themselves.
- `UNIQUE(owner_user_id, name) WHERE owner_user_id IS NOT NULL` — each user
  unique within their own set.

A plain `UNIQUE(owner_user_id, name)` would not enforce global uniqueness:
Postgres treats `NULL`s as distinct, so two globals (both
`owner_user_id IS NULL`) named `research` would slip through. The two partial
indexes give exactly the required rule — globals unique, each user's set
unique, a user free to reuse a global name (which is what "owned shadows
global" needs).

The SQLAlchemy `Skill` model's `__table_args__` is updated to match, and the
existing global unique constraint is removed there too.

## Resolution & run-loop changes (`rehketo/mcp/skills.py`)

- **Shadowing + de-dup.** `resolve_skills` de-dupes the combined (owned ∪
  global-role-allowed) set by `name` with **owned > global** precedence. The
  result: a user's `research` replaces the global `research` in their runs, and
  exactly one `/skills/{name}/SKILL.md` file (doc) or one subagent (mcp) is ever
  produced for a given name. De-dup is across the whole resolved set so a name
  shared between a doc and an mcp skill cannot collide in the agent namespace.
- **YAML-safe frontmatter (mandatory now).** `doc_skill_files` currently emits
  unquoted `name:` / `description:` lines. Once users type their own `trigger`,
  a value containing `:`, `"`, or a newline would break `SkillsMiddleware`
  frontmatter parsing. Fix: **JSON-encode the scalar values**
  (`description: {json.dumps(trigger)}`). JSON is a valid YAML subset, so the
  emitted scalar is always well-formed — and this adds no new dependency. A
  regression test round-trips a hostile trigger through deepagents'
  `file_data_to_string` / frontmatter parse.

## Backend HTTP

Two routers, following the existing `/me/*` (self-scoped) vs `/admin/*`
(privileged) convention.

### Permissions (`rehketo/permissions/actions.py`, `roles.py`)

- New action `chat.author_skill` — granted to User, Moderator, Admin.
- New action `admin.manage_skills` — granted to exactly the roles that already
  hold `admin.manage_mcp_servers` (mirror it).
- Both surfaced in `GET /me/capabilities` so the UI gates sections.

### `/me/skills` (`rehketo/api/skills_me.py`)

- `GET /me/skills` → the resolved, role-aware list for the caller. Each item
  carries `kind`, `trigger`, `display_name`, `instructions`, `enabled`, a
  `source` (`global` | `owned`), and an `editable` flag (`true` only for the
  caller's own doc-skills). This is the read behind the whole page and the
  direct answer to "visibility".
- `POST /me/skills` → create a doc-skill owned by the caller. `kind` forced to
  `doc`, `owner_user_id` forced to the caller. Body: `name`, `display_name?`,
  `trigger`, `instructions`, `enabled`.
- `PATCH /me/skills/{id}` / `DELETE /me/skills/{id}` → only when the row is
  owned by the caller and `kind == 'doc'`; otherwise 404 (don't leak
  existence). `name` is immutable after create (matches MCP-server form
  behavior); patch covers `display_name`, `trigger`, `instructions`, `enabled`.
- All `/me/skills` writes gated by `chat.author_skill`.

### `/admin/skills` (`rehketo/api/skills_admin.py`)

Parallels `rehketo/api/mcp_servers.py` (reuse the `_to_out` / `_get_or_404` /
role-validation helper shapes).

- `GET /admin/skills` → all skills.
- `POST /admin/skills` → create a global doc or global mcp skill. mcp-skills
  require an `mcp_server_id` referencing an existing server; the kind-backing
  XOR (mcp ⇒ server set, no instructions; doc ⇒ instructions, no server) is
  validated in the Pydantic schema, mirroring the DB check constraint.
- `PATCH /admin/skills/{id}` / `DELETE /admin/skills/{id}` → `name` and `kind`
  immutable on patch; patch covers `display_name`, `trigger`, `instructions`
  (doc), `mcp_server_id` (mcp), `allowed_roles`, `enabled`.
- Gated by `admin.manage_skills`.

### Shared rules

- Name validation reuses the MCP-server pattern
  (`^[a-z0-9]+([_-][a-z0-9]+)*$`, ≤64).
- Per-owner uniqueness violations (IntegrityError) surface as **409**.
- New endpoints update `rehketo-ui/openapi.snapshot.json` via the contract
  check.

## Frontend — `/settings/skills`

New route `src/routes/(app)/settings/skills/{+page.svelte,+page.ts}`.

- **Load** always calls `GET /me/skills`; when
  `auth.can('admin.manage_skills')`, it additionally fetches `GET
  /admin/skills` and `GET /admin/mcp-servers` (the latter feeds the mcp-skill
  server picker).
- **Three stacked sections, capability-gated:**
  1. *Skills available to you* — read-only cards from `/me/skills`, with
     `source` and `kind` badges.
  2. *Your skills* — the caller's owned doc-skills with edit/delete plus a
     "New skill" form. Shown when `auth.can('chat.author_skill')`.
  3. *Manage global / mcp skills* — full admin CRUD list. Shown when
     `auth.can('admin.manage_skills')`.
- **New components:** `src/lib/components/SkillForm.svelte` and a
  `src/lib/skill-form.ts` patch-body helper, modeled on `McpServerForm.svelte`
  / `mcp-server-form.ts`. The form adapts: doc mode = trigger + instructions;
  admin mcp mode = server picker + trigger + `allowed_roles`.
- A link to `/settings/skills` is added to the main `/settings` page.
- Styling reuses existing Tailwind tokens only.

## Error handling

Validate at the HTTP boundary (Pydantic schemas: name pattern, kind XOR,
required fields). Ownership and capability checks live in the route
dependencies. Internal invariants (e.g. a resolved skill's server is reachable)
are already handled by `resolve_skills` and are trusted downstream. 409 on
unique conflict, 404 on cross-owner access, 403 from the capability gate.

## Testing & validation

- **Unit:** shadowing/de-dup precedence in `resolve_skills`; YAML-safe
  frontmatter (a trigger with `:` / newline / quote round-trips through
  `file_data_to_string`); schema validators (name pattern, kind XOR);
  per-owner uniqueness → 409.
- **Route:** `/me/skills` ownership enforcement (cannot touch globals or other
  users' skills; `kind` forced `doc`); `/admin/skills` CRUD and permission
  gates; capability presence in `/me/capabilities`.
- **e2e (offline browser):** the `/settings/skills` flows — view available
  skills, create/edit/delete an own doc-skill, admin CRUD. AGENTS.md requires
  running `-m e2e` whenever wire shapes or UI flows change.
- **Contract:** regenerate `openapi.snapshot.json`.
- Run the full repo + api + ui validation blocks from AGENTS.md and quote real
  output before claiming done.

## Implementation slicing

One spec, plan ordered so value lands early:

1. **Schema + resolution** — migration `0015`, model `__table_args__`,
   shadowing/de-dup, YAML-safe frontmatter, unit tests. Retires the two latent
   bugs; ships no UI.
2. **Visibility** — `GET /me/skills`, the read-only "available to you" view,
   the two new capabilities. Delivers the core "no visibility" fix on its own.
3. **User authoring** — `/me/skills` write CRUD, `chat.author_skill`, the "Your
   skills" section and `SkillForm`.
4. **Admin management** — `/admin/skills` CRUD, `admin.manage_skills`, the
   admin section and the mcp-skill server picker.

## Out of scope (deferred)

- **User-authored mcp-skills** — a user wrapping an onboarded server into their
  own skill. Long-term goal; later phase.
- **Transcript rendering of subagent activity** — still deferred from M4.5.
- **The Settings design refresh** — the separate phase that follows this work.
