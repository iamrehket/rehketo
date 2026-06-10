# Move OAuth `next` to server-side pending-login state

**Date:** 2026-06-09
**Status:** Approved, pending implementation plan
**Follows:** `2026-06-09-codeql-alert-structural-fixes-design.md` (PR #39, merged)

## Problem

After PR #39 merged, the `push: main` CodeQL scan closed five of the six alerts
but **reopened `py/cookie-injection` as alert #7** at `auth_routes.py:50`. The
Task-3 fix (`quote()`/`unquote()` around the `next` cookie value) did not clear
it: empirically, the merged code applies `quote(next)` yet CodeQL still flags the
`set_cookie` sink, proving `urllib.parse.quote` is **not** a barrier this query
recognizes. (The PR-ref scan reported `results=0` because PR runs are
diff-informed against the base and the alert sits on an unchanged line — only the
authoritative `push: main` run surfaced it.)

The stored value is genuinely safe (`_is_safe_next`-validated and percent-encoded
— it cannot inject cookie attributes), so this is a true false positive. Rather
than dismiss it, we remove the **source** of the taint: stop putting any
user-supplied value in a cookie at all.

## Goal

`py/cookie-injection` closes on the next `push: main` CodeQL scan because no
user-controlled data flows to any `set_cookie` sink — the taint path is gone by
construction, not sanitized. The change is also a genuine improvement: transient
login state moves to a single-use, TTL-bounded server-side row.

## Scope decision

The OAuth login→callback round-trip carries three values, all currently in
short-lived cookies:

- `state` — app-generated anti-CSRF token (not user input, **not** flagged)
- `code_verifier` — app-generated PKCE secret (not user input, **not** flagged)
- `next` — user-supplied (`?next=`) post-login path (**the flagged value**)

Only `next` is moved server-side. The `state` and `code_verifier` cookies are
left exactly as they are: they are not user input, not flagged, and the verifier
cookie is seeded by four test files — relocating it would be a large out-of-scope
change for no alert benefit (charter rule 7). Moving the verifier server-side for
defense-in-depth is a reasonable **separate** follow-up, not part of this fix.

## Design

**Principle:** remove the tainted source, don't sanitize the sink. The browser
keeps only the random `state` (the CSRF binding + correlation key); `next` lives
in a server-side row keyed by that `state`, consumed once in the callback.

### New table `oauth_pending_logins` (model `PendingLogin`)

| column | type | notes |
|--------|------|-------|
| `state` | `Text` | primary key — the `secrets.token_urlsafe(24)` login token |
| `next_path` | `Text` null | the validated post-login path |
| `created_at` | `timestamptz` | `server_default=func.now()` |
| `expires_at` | `timestamptz` not null | now + 600s (matches the old cookie `max_age`) |

Migration `0007_oauth_pending_logins.py` (revision `"0007"`, down_revision
`"0006"`) creates the table in `upgrade` and drops it in `downgrade` (the test
fixture downgrades to base each run, so `downgrade` must be correct).

### New store module `rehketo/auth/login_state.py` (mirrors `sessions.py`)

- `async def create_pending_login(db, *, state: str, next_path: str, ttl_seconds: int) -> None`
  — insert a row; also `DELETE FROM oauth_pending_logins WHERE expires_at < now()`
  in the same unit of work to self-prune abandoned logins (keeps the table
  bounded without a separate sweeper).
- `async def consume_pending_login(db, state: str) -> str | None`
  — `DELETE ... WHERE state = :state RETURNING next_path, expires_at`
  (single-use), returning `next_path` only when the row existed and had not
  expired; otherwise `None`.

### `auth_routes.py` changes

- `login` gains a `db: Annotated[AsyncSession, Depends(db_session)]` dependency.
  When `next is not None and _is_safe_next(next)`, it calls
  `await create_pending_login(db, state=start.state, next_path=next, ttl_seconds=600)`
  **instead of** setting the `rehketo_oauth_next` cookie. (No DB write for the
  common no-`next` login.)
- `callback` drops the `rehketo_oauth_next` cookie parameter. After the existing
  `state == rehketo_oauth_state` CSRF check, it does
  `next_path = await consume_pending_login(db, state)` and passes that to
  `_resolve_post_login_target`. The `rehketo_oauth_next` cookie deletion is
  removed.
- Revert the now-pointless `quote()`/`unquote()` on `next` (it never touches a
  cookie now). Remove the `OAUTH_NEXT_COOKIE` constant. Drop the `unquote`
  import; **keep** `quote` (still used in `_oauth_error_redirect`).
- `_resolve_post_login_target` and `_is_safe_next` are unchanged — `next` is
  still validated by `_is_safe_next` at login before storage, and re-validated by
  the resolver in the callback (defense-in-depth).

### Why CodeQL clears

No user-supplied value reaches any `set_cookie` call: the only cookies set are
`state` and `code_verifier`, both app-generated. The taint path to line 50 no
longer exists.

### CSRF preserved

The row is keyed by `state`, but the callback still requires the `state` *cookie*
to match the `state` query param (the existing check) before the row is read, so
the current CSRF protection is unchanged.

## Tests

- `test_auth_next_preservation.py` is rewritten to use the server-side row via the
  existing `db` async-session fixture:
  - login-with-safe-`next`: assert a `PendingLogin` row exists for the response's
    `rehketo_oauth_state` cookie value, with `next_path` equal to the input, and
    assert **no** `rehketo_oauth_next` cookie is set.
  - login-with-unsafe-`next`: assert **no** row is created.
  - callback round-trip: seed a `PendingLogin` row (state `"s1"`, a safe
    `next_path`), call callback, assert redirect to that path on the UI origin,
    and assert the row was consumed (deleted).
  - callback defense-in-depth: seed a row whose `next_path` is unsafe (e.g.
    `//evil.example.com/pwn`), assert the callback falls back to the configured
    post-login URL (the resolver re-validates).
- `tests/unit/test_models_compile.py`: add `oauth_pending_logins` to the required
  table-name set.
- The other three callback test files (`test_auth_callback_error.py`,
  `test_auth_default_role.py`, `test_auth_oauth_callback.py`) only seed
  `state`+`verifier` cookies and pass no `next` — they are **untouched**.

## Out of scope

- Moving `code_verifier` server-side (separate defense-in-depth follow-up).
- A periodic sweeper for expired rows (the insert-time prune + single-use consume
  keep the table bounded; a scheduled sweep can be added later if needed).

## Verification

Local (from `rehketo-api/`): `ruff format --check`, `ruff check`, `mypy rehketo`,
`bandit -r rehketo`, `lint-imports`, `pytest`. Repo guards from root:
`tools/agent_guards.py check`. CodeQL only confirms on `push: main`, so the work
is not done until PR merge + the `main` scan shows alert #7 `fixed` and no new
alert in its place.
