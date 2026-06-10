# CodeQL Alert Structural Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all six open CodeQL alerts in `rehketo-api` by re-expressing existing security guards in analyzer-recognized forms (framework primitives + value transformations), keeping all existing tests green.

**Architecture:** Four independent structural edits. (1) Replace the hand-rolled SPA catch-all route in `main.py` with a `StaticFiles` subclass that adds 404→index fallback — Starlette owns the secure path resolution. (2) Rebuild the post-login redirect from a constant scheme/netloc via `urlunparse`. (3) Percent-encode the `next` cookie value with `quote`/`unquote`. (4) Assert on a parsed host in one test. Each change is behavior-preserving and covered by existing tests; the security fix is *verified* by the CodeQL re-scan on push.

**Tech Stack:** Python 3.14, FastAPI/Starlette, pytest, `uv`. Spec: `docs/superpowers/specs/2026-06-09-codeql-alert-structural-fixes-design.md`.

---

## File Structure

- **Modify** `rehketo-api/rehketo/main.py` — swap the custom `_ui_catchall` route for a `_SPAStaticFiles` mount; drop the now-unused `FileResponse` import. (Fixes alerts #2,#3,#4 `py/path-injection`.)
- **Modify** `rehketo-api/rehketo/api/auth_routes.py` — `urlunparse`-based `_resolve_post_login_target` (removing orphaned `_ui_origin`); `quote`/`unquote` around the `next` cookie. (Fixes alert #1 `py/url-redirection` and #6 `py/cookie-injection`.)
- **Modify** `rehketo-api/tests/integration/test_auth_login_redirect.py` — parsed-host assertion. (Fixes alert #5 `py/incomplete-url-substring-sanitization`.)

No new files. All four sites are exercised by existing tests in `tests/integration/test_ui_static_mount.py`, `tests/integration/test_auth_next_preservation.py`, and `tests/integration/test_auth_login_redirect.py`.

---

## Task 1: Path-injection — StaticFiles SPA mount (`main.py`)

**Files:**
- Modify: `rehketo-api/rehketo/main.py:11` (remove `FileResponse` import)
- Modify: `rehketo-api/rehketo/main.py:136-169` (`_mount_ui_static_bundle_if_configured`)
- Test (existing, must stay green): `rehketo-api/tests/integration/test_ui_static_mount.py`

- [ ] **Step 1: Establish the baseline — run the existing mount tests**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_ui_static_mount.py -v
```
Expected: PASS (3 tests). This is the regression net the refactor must preserve.

- [ ] **Step 2: Add the StaticFiles imports**

In `rehketo-api/rehketo/main.py`, the FastAPI response import block currently reads:
```python
from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse
```
Replace those three lines with:
```python
from fastapi import Depends, FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope
```
(`FileResponse` is removed because the custom route that used it is being deleted — leaving it would be an unused import, charter rule 8.)

- [ ] **Step 3: Replace the custom catch-all with a SPA StaticFiles subclass**

In `rehketo-api/rehketo/main.py`, replace the inner route block of `_mount_ui_static_bundle_if_configured` — the lines:
```python
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _ui_catchall(full_path: str) -> FileResponse:
        if full_path:
            candidate = (ui_dir / full_path).resolve()
            # Reject traversal: candidate must remain under ui_dir.
            if candidate.is_file() and candidate.is_relative_to(ui_dir.resolve()):
                return FileResponse(candidate)
        return FileResponse(index_html)
```
with:
```python
    app.mount("/", _SPAStaticFiles(directory=ui_dir, html=True), name="ui")
```
The `index_html` existence guard above it stays (it preserves the "warn and skip when index.html is missing" contract). The `ui_dir`/`is_dir` guards also stay unchanged.

- [ ] **Step 4: Define the `_SPAStaticFiles` subclass**

Add this class to `rehketo-api/rehketo/main.py` immediately above `_mount_ui_static_bundle_if_configured` (so it is defined before use):
```python
class _SPAStaticFiles(StaticFiles):
    """Serve the built SvelteKit bundle, falling back to index.html for any
    path Starlette can't resolve to a real file — so client-side routes (like
    /c/<uuid>) survive a full page load. StaticFiles does its own realpath-based
    containment check, so user input never touches a path join we own."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
```

- [ ] **Step 5: Run the mount tests to verify behavior is preserved**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_ui_static_mount.py -v
```
Expected: PASS (3 tests) — root serves index, unknown route falls back to index, real assets serve directly, API routes still win, no-op when env unset.

- [ ] **Step 6: Lint/type-check the file**

Run (from `rehketo-api/`):
```bash
uv run ruff check rehketo/main.py && uv run mypy rehketo/main.py
```
Expected: no errors. (If mypy flags the `scope: Scope` override signature, confirm it matches `StaticFiles.get_response`; it does in Starlette's current release.)

- [ ] **Step 7: Commit**

```bash
git add rehketo-api/rehketo/main.py
git commit -m "fix: serve UI bundle via StaticFiles SPA mount

Replaces the hand-rolled catch-all path join (which CodeQL flagged as
py/path-injection despite the is_relative_to guard) with a StaticFiles
subclass. Starlette owns the secure path resolution; user input no longer
reaches a Path join we own."
```

---

## Task 2: Open-redirect — constant-netloc redirect (`auth_routes.py`)

**Files:**
- Modify: `rehketo-api/rehketo/api/auth_routes.py:59-74` (`_ui_origin` removed, `_resolve_post_login_target` rebuilt)
- Test (existing, must stay green): `rehketo-api/tests/integration/test_auth_next_preservation.py`

- [ ] **Step 1: Establish the baseline**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_auth_next_preservation.py -v
```
Expected: PASS. `test_callback_uses_next_cookie_when_safe` pins the exact target `http://127.0.0.1:5173/c/deep-link`, which the refactor must reproduce.

- [ ] **Step 2: Remove the now-orphaned `_ui_origin` helper and rebuild the resolver**

In `rehketo-api/rehketo/api/auth_routes.py`, replace this block:
```python
def _ui_origin() -> str:
    """Origin (scheme://host[:port]) of the UI, derived from ui_post_login_url."""
    parsed = urlparse(get_settings().ui_post_login_url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _resolve_post_login_target(next_path: str | None) -> str:
    """Return an absolute URL on the UI origin.

    `next_path` is trusted only when it is a relative path starting with a
    single `/` — never a protocol-relative `//evil.com/...` or full URL.
    Falls back to the configured `ui_post_login_url` on anything suspicious.
    """
    if next_path and _is_safe_next(next_path):
        return urljoin(_ui_origin() + "/", next_path.lstrip("/"))
    return get_settings().ui_post_login_url
```
with:
```python
def _resolve_post_login_target(next_path: str | None) -> str:
    """Return an absolute URL on the UI origin.

    The scheme and host come *only* from the configured `ui_post_login_url`;
    `next_path` (when it passes `_is_safe_next`) supplies just the path
    component. Because the netloc is never derived from user input, the
    redirect can't be steered off-origin. Falls back to the configured
    post-login URL on anything suspicious.
    """
    if next_path and _is_safe_next(next_path):
        origin = urlparse(get_settings().ui_post_login_url)
        return urlunparse((origin.scheme, origin.netloc, next_path, "", "", ""))
    return get_settings().ui_post_login_url
```
Note: `urljoin` is no longer used after this change — proceed to Step 3 before linting.

- [ ] **Step 3: Drop the unused `urljoin` import**

In `rehketo-api/rehketo/api/auth_routes.py`, the import line:
```python
from urllib.parse import quote, urljoin, urlparse, urlunparse
```
becomes (we lose `urljoin`, gain `unquote` which Task 3 needs):
```python
from urllib.parse import quote, unquote, urlparse, urlunparse
```

- [ ] **Step 4: Run the redirect tests to verify behavior is preserved**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_auth_next_preservation.py -v
```
Expected: PASS. In particular `test_callback_uses_next_cookie_when_safe` still asserts `location == "http://127.0.0.1:5173/c/deep-link"`, and `test_callback_rejects_protocol_relative_next_cookie` still falls back to `http://127.0.0.1:5173/`.

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/api/auth_routes.py
git commit -m "fix: build post-login redirect from constant UI netloc

Constructs the redirect via urlunparse with scheme/host taken only from
ui_post_login_url; user-supplied next only fills the path. CodeQL's
py/url-redirection can see the host is not user-controlled. Removes the
now-unused _ui_origin helper."
```

---

## Task 3: Cookie-injection — encode the `next` cookie value (`auth_routes.py`)

**Files:**
- Modify: `rehketo-api/rehketo/api/auth_routes.py:102-103` (encode on write, in `login`)
- Modify: `rehketo-api/rehketo/api/auth_routes.py:209-211` (decode on read, in `callback`)
- Test (existing, must stay green): `rehketo-api/tests/integration/test_auth_next_preservation.py`

- [ ] **Step 1: Encode the `next` value when writing the cookie**

In `rehketo-api/rehketo/api/auth_routes.py`, inside `login`, replace:
```python
    if next is not None and _is_safe_next(next):
        _set_oauth_cookie(resp, OAUTH_NEXT_COOKIE, next, secure=s.cookie_secure)
```
with:
```python
    if next is not None and _is_safe_next(next):
        # Percent-encode before storing: keeps raw path delimiters out of the
        # cookie value (CodeQL py/cookie-injection sanitizer) and is reversed
        # by unquote() in the callback.
        _set_oauth_cookie(
            resp, OAUTH_NEXT_COOKIE, quote(next), secure=s.cookie_secure
        )
```
(`quote` defaults to `safe="/"`, so a normal path like `/c/abc-123` is unchanged — the existing `test_login_sets_next_cookie_for_safe_path` assertion still holds.)

- [ ] **Step 2: Decode the `next` value when reading it in the callback**

In `rehketo-api/rehketo/api/auth_routes.py`, inside `callback`, replace:
```python
    resp = RedirectResponse(
        _resolve_post_login_target(rehketo_oauth_next), status_code=302
    )
```
with:
```python
    next_path = unquote(rehketo_oauth_next) if rehketo_oauth_next is not None else None
    resp = RedirectResponse(
        _resolve_post_login_target(next_path), status_code=302
    )
```

- [ ] **Step 3: Run the redirect tests to verify the round-trip still works**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_auth_next_preservation.py -v
```
Expected: PASS. `quote`/`unquote` is transparent for the clean paths the tests use, so `http://127.0.0.1:5173/c/deep-link` and the cookie value `/c/abc-123` are unchanged.

- [ ] **Step 4: Lint/type-check the file**

Run (from `rehketo-api/`):
```bash
uv run ruff check rehketo/api/auth_routes.py && uv run mypy rehketo/api/auth_routes.py
```
Expected: no errors, no unused-import warnings (`urljoin` gone, `unquote` now used).

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/api/auth_routes.py
git commit -m "fix: percent-encode the next cookie value

quote() on write / unquote() on read keeps raw path delimiters out of the
oauth_next cookie — a CodeQL py/cookie-injection sanitizer and independently
correct. Transparent for normal paths."
```

---

## Task 4: Substring sanitization — parsed-host assertion (test)

**Files:**
- Modify: `rehketo-api/tests/integration/test_auth_login_redirect.py:1-7` (add `urlparse` import)
- Modify: `rehketo-api/tests/integration/test_auth_login_redirect.py:22`

- [ ] **Step 1: Add the `urlparse` import**

In `rehketo-api/tests/integration/test_auth_login_redirect.py`, after the existing imports add:
```python
from urllib.parse import urlparse
```
Place it with the other stdlib import; the file's top becomes:
```python
from __future__ import annotations

from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from rehketo.main import create_app
```

- [ ] **Step 2: Replace the substring assertion with a parsed-host check**

In the same file, replace:
```python
    assert "login.microsoftonline.com" in loc
```
with:
```python
    assert urlparse(loc).hostname == "login.microsoftonline.com"
```

- [ ] **Step 3: Run the test to verify it still passes**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_auth_login_redirect.py -v
```
Expected: PASS. The Entra authorize URL host is exactly `login.microsoftonline.com`, so the stricter host-equality assertion holds.

- [ ] **Step 4: Commit**

```bash
git add rehketo-api/tests/integration/test_auth_login_redirect.py
git commit -m "test: assert Entra redirect host by parsed hostname

Replaces a substring URL check (CodeQL py/incomplete-url-substring-
sanitization) with an exact urlparse().hostname comparison."
```

---

## Task 5: Full validation + trigger the CodeQL re-scan

**Files:** none (verification only).

- [ ] **Step 1: Run the api validation block**

Run (from `rehketo-api/`):
```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
```
Expected: all pass. Quote the real `pytest` summary line in the report.

- [ ] **Step 2: Run the repo-wide guards**

Run (from repo root):
```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```
Expected: pass (no AGENTS.md edits were made, so the mirror check is clean).

- [ ] **Step 3: Push the branch to trigger CodeQL**

```bash
git push -u origin fix/codeql-alert-structural-fixes
```

- [ ] **Step 4: Wait for the scan, then confirm all six alerts closed**

After the code-scanning workflow finishes on the pushed ref, run (from repo root):
```bash
gh api 'repos/iamrehket/rehketo/code-scanning/alerts?state=open&per_page=100' \
  --jq '.[] | "#\(.number) \(.rule.id) \(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
```
Expected: the four `auth_routes.py`/`main.py`/test alerts (#1–#6) no longer appear for this branch's analysis. **Do not claim done until this output is empty for the six targeted alerts** (charter rule 5). If any survive, iterate on that specific site (e.g. open-redirect may need an explicit netloc-allowlist assertion at the redirect site) and re-push.

- [ ] **Step 5: Open the PR**

```bash
gh pr create --fill --base main
```
Reference the spec and note the Dependabot `cookie` bump is tracked separately (out of scope).

---

## Notes

- **Dependabot `cookie < 0.7.0`** (low, `rehketo-ui/pnpm-lock.yaml`) is intentionally out of scope — a transitive UI dependency bump handled separately.
- CodeQL alerts close on the analysis of the branch/PR; full closure on the repo's default view happens after merge to `main`.
