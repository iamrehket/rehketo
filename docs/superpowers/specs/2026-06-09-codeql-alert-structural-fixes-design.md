# Resolving the open CodeQL alerts via structural fixes

**Date:** 2026-06-09
**Status:** Approved, pending implementation plan

## Problem

GitHub code scanning (CodeQL) reports six open alerts against `rehketo-api`,
plus one low Dependabot alert. Triage of the flagged code shows the six CodeQL
findings are **false positives or test-only**: the defenses CodeQL is looking
for already exist, but they are expressed as *boolean guards* that CodeQL's
taint queries do not model as sanitizers. The fix is not to add security
controls — it is to re-express the existing controls in a form the analyzer
recognizes, which also makes the code more robust and self-documenting.

| # | Alert | Location | Why it's flagged |
|---|-------|----------|------------------|
| 2,3,4 | `py/path-injection` (error) | `rehketo/main.py:165–168` | Hand-rolled SPA catch-all joins user `full_path` onto `ui_dir`; the `candidate.is_relative_to(ui_dir.resolve())` containment check is correct but `is_relative_to` is not in CodeQL's sanitizer model. |
| 1 | `py/url-redirection` (error) | `rehketo/api/auth_routes.py:210` | Post-login redirect target derives from the `rehketo_oauth_next` cookie via `_resolve_post_login_target` → `_is_safe_next`; CodeQL cannot trace taint through the predicate helper. |
| 6 | `py/cookie-injection` (warning) | `rehketo/api/auth_routes.py:50` | `_set_oauth_cookie` stores the `next` value; it is validated by `_is_safe_next` before reaching the cookie, but predicate validation is not a recognized sanitizer. |
| 5 | `py/incomplete-url-substring-sanitization` (warning) | `tests/integration/test_auth_login_redirect.py:22` | Test asserts `"login.microsoftonline.com" in loc` — a substring check on a URL, flagged as a weak pattern even though it is a test assertion, not a runtime control. |

The Dependabot alert (`cookie < 0.7.0`, transitive in `rehketo-ui/pnpm-lock.yaml`,
low severity) is **out of scope** for this design; it is a UI dependency bump
tracked separately.

## Goal

Every open CodeQL alert resolves on the next scan because the underlying code is
provably safe in a form the analyzer recognizes — no dismissals, no inline
suppression escape hatches (charter rule 6). Each change must also stand on its
own as a maintainability improvement, per the project's preference for
structural fixes over minimal patches.

## Design

The unifying principle: **replace each predicate-style guard CodeQL cannot see
through with a value transformation or framework primitive CodeQL already
trusts.** Static analyzers reason about data shape, not about whether a prior
`if` returned — transformations (`quote`, `urlunparse`, framework path
resolution, parsed-host comparison) are visible to them; boolean predicates
often are not.

### Fix 1 — Path-injection (`main.py`)

Replace the hand-rolled `@app.get("/{full_path:path}")` catch-all and its custom
traversal guard with a `StaticFiles` subclass that adds SPA fallback:

```python
class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response
```

Mounted at `/` **after** the API routers (preserving the documented ordering so
auth/conversations/runs/docs/openapi/healthz still win). Starlette's
`StaticFiles` performs its own `realpath`-based containment check internally;
user input never reaches a raw `Path` join we own, so CodeQL raises nothing.
This is the `StaticFiles`-based serving the repo's guide already describes, and
it deletes the custom guard entirely — fewer lines, more robust, clears 3 of 6
alerts.

**Constraint:** existing e2e/integration tests around SPA fallback (real files
resolve directly; unknown client routes like `/c/<uuid>` return `index.html`;
API paths are untouched) must stay green. The `index.html`/`is_dir` guard
behavior in `_mount_ui_static_bundle_if_configured` (no-op when `UI_STATIC_DIR`
unset, warnings when the dir or index is missing) is preserved.

### Fix 2 — Open-redirect (`auth_routes.py`)

Rebuild the redirect target in `_resolve_post_login_target` so user input
supplies only the **path** component, assembled via
`urlunparse((scheme, netloc, path, "", "", ""))` where `scheme` and `netloc`
come from the trusted UI origin as constants. CodeQL's redirect query tracks
taint into the netloc; a provably-constant netloc removes the off-origin flow.
`_is_safe_next` is retained as defense-in-depth.

### Fix 3 — Cookie-injection (`auth_routes.py`)

Percent-encode the `next` value at the cookie boundary: `quote` it when writing
the `OAUTH_NEXT_COOKIE` in `login`, `unquote` it when reading the cookie in
`callback` before it reaches `_resolve_post_login_target`. `quote` is a
CodeQL-recognized cookie-injection sanitizer and is independently correct — a
cookie value should not carry raw path delimiters. The app-generated `state` and
`code_verifier` values are URL-safe already and are left untouched.

### Fix 4 — Substring sanitization (test)

Assert on the parsed host instead of a substring:
`urlparse(loc).hostname == "login.microsoftonline.com"`. This is the exact
recommended remediation and is a stricter assertion than the substring check.

## Out of scope

- The Dependabot `cookie` bump (UI dependency, separate change).
- Any unrelated refactor of `auth_routes.py` or `main.py` beyond the four sites
  above (charter rule 7).

## Verification

Local (from `rehketo-api/`): `uv run ruff format --check`, `uv run ruff check`,
`uv run mypy rehketo`, `uv run bandit -r rehketo`, `uv run lint-imports`,
`uv run pytest`. Repo-wide guards from root:
`uv run --project rehketo-api python tools/agent_guards.py check`.

CodeQL re-runs only on push. The work is **not** "done" (charter rule 5) until a
branch push triggers a scan that closes all six alerts. Any alert that survives
is iterated on at its specific site before merge.
