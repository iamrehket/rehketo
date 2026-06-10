# OAuth `next` Server-Side State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the user-supplied OAuth `next` path out of a browser cookie into a single-use, TTL-bounded server-side row, eliminating the `py/cookie-injection` taint path (alert #7) by construction.

**Architecture:** New `oauth_pending_logins` table (model + Alembic migration `0007`) keyed by the random `state` token; a `login_state.py` store module (`create_pending_login`/`consume_pending_login`); `login` writes the row instead of a `next` cookie, `callback` consumes it. `state`/`verifier` cookies are untouched.

**Tech Stack:** Python 3.14, FastAPI/Starlette, async SQLAlchemy + Alembic, Postgres (testcontainers), pytest, `uv`. Spec: `docs/superpowers/specs/2026-06-09-oauth-next-server-side-design.md`.

---

## File Structure

- **Modify** `rehketo-api/rehketo/db/models.py` — add `PendingLogin` model (table `oauth_pending_logins`).
- **Create** `rehketo-api/alembic/versions/0007_oauth_pending_logins.py` — create/drop the table.
- **Create** `rehketo-api/rehketo/auth/login_state.py` — the store module.
- **Modify** `rehketo-api/rehketo/api/auth_routes.py` — `login` writes the row; `callback` consumes it; revert `quote`/`unquote`; drop `OAUTH_NEXT_COOKIE`.
- **Modify** `rehketo-api/tests/unit/test_models_compile.py` — add the new table name.
- **Modify** `rehketo-api/tests/integration/test_auth_next_preservation.py` — rewrite around the server-side row.

Tasks are sequential (each depends on the prior).

---

## Task 1: `PendingLogin` model + migration + models-compile test

**Files:**
- Modify: `rehketo-api/rehketo/db/models.py`
- Create: `rehketo-api/alembic/versions/0007_oauth_pending_logins.py`
- Modify: `rehketo-api/tests/unit/test_models_compile.py`

- [ ] **Step 1: Add the `PendingLogin` model**

In `rehketo-api/rehketo/db/models.py`, add after the `Session` class (it groups with the other auth tables):
```python
class PendingLogin(Base):
    __tablename__ = "oauth_pending_logins"

    # The random `state` token (secrets.token_urlsafe) that correlates a login
    # redirect with its callback. Server-side home for the user-supplied `next`
    # path so it never rides in a browser cookie.
    state: Mapped[str] = mapped_column(Text, primary_key=True)
    next_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```
(`Text`, `DateTime`, `func`, `Mapped`, `mapped_column` are already imported in this file.)

- [ ] **Step 2: Write the migration**

Create `rehketo-api/alembic/versions/0007_oauth_pending_logins.py`:
```python
"""server-side store for the OAuth post-login `next` path

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-09 00:00:00.000000+00:00

Moves the user-supplied `next` path out of a browser cookie (which CodeQL
flagged as py/cookie-injection) into a single-use, TTL-bounded row keyed by
the login `state` token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_pending_logins",
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("next_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )


def downgrade() -> None:
    op.drop_table("oauth_pending_logins")
```

- [ ] **Step 3: Add the table to the models-compile test**

In `rehketo-api/tests/unit/test_models_compile.py`, add `"oauth_pending_logins"` to the `required` set (alongside the existing names).

- [ ] **Step 4: Verify the migration applies and the model compiles**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/unit/test_models_compile.py -v
uv run pytest tests/integration/test_ui_static_mount.py::test_mount_is_noop_when_env_unset -v
```
The second is a cheap integration test that exercises the `db_url` fixture's full
`downgrade base` → `upgrade head` cycle, proving migration `0007` upgrades and
downgrades cleanly. Expected: PASS.

- [ ] **Step 5: Lint/type-check + commit**

Run (from `rehketo-api/`):
```bash
uv run ruff check rehketo/db/models.py alembic/versions/0007_oauth_pending_logins.py tests/unit/test_models_compile.py
uv run mypy rehketo/db/models.py
```
Then:
```bash
git add rehketo-api/rehketo/db/models.py rehketo-api/alembic/versions/0007_oauth_pending_logins.py rehketo-api/tests/unit/test_models_compile.py
git commit -m "feat: add oauth_pending_logins table for server-side next state"
```

---

## Task 2: `login_state.py` store module

**Files:**
- Create: `rehketo-api/rehketo/auth/login_state.py`

- [ ] **Step 1: Write the store module**

Create `rehketo-api/rehketo/auth/login_state.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert

from rehketo.db.models import PendingLogin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def create_pending_login(
    db: AsyncSession, *, state: str, next_path: str, ttl_seconds: int
) -> None:
    """Persist the post-login `next` path keyed by the login `state` token.

    Also prunes any already-expired rows so abandoned logins don't accumulate —
    cheap because the table only ever holds in-flight logins.
    """
    now = datetime.now(UTC)
    await db.execute(delete(PendingLogin).where(PendingLogin.expires_at < now))
    await db.execute(
        insert(PendingLogin).values(
            state=state,
            next_path=next_path,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    await db.commit()


async def consume_pending_login(db: AsyncSession, state: str) -> str | None:
    """Single-use lookup: delete the row for `state` and return its `next_path`,
    but only when the row existed and had not expired. Returns None otherwise so
    the caller falls back to the default post-login URL."""
    now = datetime.now(UTC)
    result = await db.execute(
        delete(PendingLogin)
        .where(PendingLogin.state == state)
        .returning(PendingLogin.next_path, PendingLogin.expires_at)
    )
    row = result.first()
    await db.commit()
    if row is None or row.expires_at <= now:
        return None
    return row.next_path
```

- [ ] **Step 2: Lint/type-check**

Run (from `rehketo-api/`):
```bash
uv run ruff check rehketo/auth/login_state.py && uv run mypy rehketo/auth/login_state.py && uv run lint-imports
```
Expected: clean (the import-linter contract `auth and permissions never depend on api` must stay KEPT — this module imports only from `rehketo.db`, which is allowed).

- [ ] **Step 3: Commit**

```bash
git add rehketo-api/rehketo/auth/login_state.py
git commit -m "feat: add pending-login store (create/consume next by state)"
```

---

## Task 3: Wire `auth_routes.py` to the store

**Files:**
- Modify: `rehketo-api/rehketo/api/auth_routes.py`

- [ ] **Step 1: Update imports**

In `rehketo-api/rehketo/api/auth_routes.py`:
- Change `from urllib.parse import quote, unquote, urlparse, urlunparse` to
  `from urllib.parse import quote, urlparse, urlunparse` (drop `unquote`; `quote`
  stays — used in `_oauth_error_redirect`).
- Add `from rehketo.auth import login_state` next to the existing
  `from rehketo.auth import sessions as session_store` import.

- [ ] **Step 2: Remove the `OAUTH_NEXT_COOKIE` constant**

Delete the line:
```python
OAUTH_NEXT_COOKIE = "rehketo_oauth_next"
```
(Leave `OAUTH_STATE_COOKIE` and `OAUTH_VERIFIER_COOKIE`.)

- [ ] **Step 3: Rewrite the `next` handling in `login`**

`login` currently has no `db` dependency. Change its signature from:
```python
@router.get("/login")
async def login(
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    s = get_settings()
```
to:
```python
@router.get("/login")
async def login(
    db: Annotated[AsyncSession, Depends(db_session)],
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    s = get_settings()
```
Then replace the cookie-writing block:
```python
    if next is not None and _is_safe_next(next):
        # Percent-encode before storing: escapes cookie-metadata delimiters
        # (`;`, `,`, CR/LF) that could otherwise inject extra Set-Cookie
        # attributes; `/` is left raw. Reversed by unquote() in the callback.
        # CodeQL py/cookie-injection sanitizer.
        _set_oauth_cookie(resp, OAUTH_NEXT_COOKIE, quote(next), secure=s.cookie_secure)
    return resp
```
with:
```python
    if next is not None and _is_safe_next(next):
        # Persist the intended post-login path server-side, keyed by the login
        # `state` token, so no user-supplied value ever rides in a cookie.
        await login_state.create_pending_login(
            db, state=start.state, next_path=next, ttl_seconds=600
        )
    return resp
```

- [ ] **Step 4: Rewrite the `next` handling in `callback`**

Remove the `rehketo_oauth_next` cookie parameter from `callback`'s signature
(delete this parameter line):
```python
    rehketo_oauth_next: Annotated[str | None, Cookie(alias=OAUTH_NEXT_COOKIE)] = None,
```
Replace the redirect-building block:
```python
    next_path = unquote(rehketo_oauth_next) if rehketo_oauth_next is not None else None
    resp = RedirectResponse(
        _resolve_post_login_target(next_path), status_code=302
    )
```
with:
```python
    next_path = await login_state.consume_pending_login(db, state)
    resp = RedirectResponse(_resolve_post_login_target(next_path), status_code=302)
```
And remove the next-cookie deletion line:
```python
    resp.delete_cookie(OAUTH_NEXT_COOKIE, path="/auth/")
```
(Keep the `OAUTH_STATE_COOKIE` and `OAUTH_VERIFIER_COOKIE` deletions.)

- [ ] **Step 5: Lint/type-check**

Run (from `rehketo-api/`):
```bash
uv run ruff check rehketo/api/auth_routes.py && uv run mypy rehketo/api/auth_routes.py && uv run lint-imports
```
Expected: clean, no unused imports (`unquote` gone, `OAUTH_NEXT_COOKIE` gone), no `Cookie` left unused (it's still used for state/verifier).

- [ ] **Step 6: Commit**

```bash
git add rehketo-api/rehketo/api/auth_routes.py
git commit -m "fix: carry OAuth next via server-side pending-login, not a cookie

Removes the user-supplied next value from all Set-Cookie sinks (the
py/cookie-injection taint source). login writes a pending-login row keyed
by state; callback consumes it. Reverts the ineffective quote/unquote."
```

---

## Task 4: Rewrite `test_auth_next_preservation.py`

**Files:**
- Modify: `rehketo-api/tests/integration/test_auth_next_preservation.py`

Context: the existing file already has `_fake_id_token()` and `_token_response()`
helpers and uses `@respx.mock` for the token endpoint. A `db` async-session
fixture is available (from `tests/conftest.py`) to seed/inspect rows. Import the
model and helpers as needed.

- [ ] **Step 1: Add imports for the model, store-free DB access, and time**

At the top of the test file, add:
```python
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from rehketo.db.models import PendingLogin
```
(Keep the existing imports.)

- [ ] **Step 2: Replace the two login tests**

Replace `test_login_sets_next_cookie_for_safe_path` and
`test_login_percent_encodes_next_cookie` (the cookie-asserting login tests) with a
single row-asserting login test:
```python
@pytest.mark.asyncio
async def test_login_persists_next_server_side_for_safe_path(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get("/auth/login", params={"next": "/c/abc-123"})
    assert r.status_code == 302
    # No user-supplied value rides in a cookie anymore.
    assert "rehketo_oauth_next=" not in r.headers.get("set-cookie", "")
    # The path is persisted server-side, keyed by the state token the browser got.
    state = r.cookies.get("rehketo_oauth_state")
    assert state
    row = (
        await db.execute(select(PendingLogin).where(PendingLogin.state == state))
    ).scalar_one()
    assert row.next_path == "/c/abc-123"
```
Import `AsyncSession` for the annotation: add
`from sqlalchemy.ext.asyncio import AsyncSession` to the imports if not present.

- [ ] **Step 3: Keep the unsafe-`next` login test, asserting no row**

Replace `test_login_ignores_unsafe_next` so it asserts no row is created (keep the
same parametrize list):
```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "//evil.example.com/x",
        "http://evil.example.com/",
        "/\\evil",
        "no-leading-slash",
        "",
    ],
)
async def test_login_ignores_unsafe_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession, unsafe: str
) -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get("/auth/login", params={"next": unsafe})
    assert r.status_code == 302
    assert "rehketo_oauth_next=" not in r.headers.get("set-cookie", "")
    count = (await db.execute(select(PendingLogin))).all()
    assert count == []
```

- [ ] **Step 4: Replace the callback round-trip test to seed a row**

Replace `test_callback_uses_next_cookie_when_safe` and
`test_callback_decodes_percent_encoded_next_cookie` with one row-seeded round-trip
(seed a `PendingLogin` row, then call callback with only the state cookie):
```python
@pytest.mark.asyncio
@respx.mock
async def test_callback_uses_pending_login_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    token_url = f"{authority()}/oauth2/v2.0/token"
    respx.post(token_url).mock(
        return_value=respx.MockResponse(200, json=_token_response())
    )
    db.add(
        PendingLogin(
            state="s1",
            next_path="/c/deep-link",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get(
            "/auth/callback",
            params={"code": "abc", "state": "s1"},
            cookies={"rehketo_oauth_state": "s1", "rehketo_oauth_verifier": "v1"},
        )
    assert r.status_code == 302
    assert r.headers["location"] == "http://127.0.0.1:5173/c/deep-link"
    # Single-use: the row is consumed.
    remaining = (await db.execute(select(PendingLogin))).all()
    assert remaining == []
```

- [ ] **Step 5: Replace the protocol-relative defense-in-depth test**

Replace `test_callback_rejects_protocol_relative_next_cookie` with a row-seeded
version (seed an unsafe `next_path`, assert fallback):
```python
@pytest.mark.asyncio
@respx.mock
async def test_callback_rejects_unsafe_pending_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    # Defense in depth: even if an unsafe value were somehow stored, the
    # callback's resolver re-validates and falls back to the configured URL.
    token_url = f"{authority()}/oauth2/v2.0/token"
    respx.post(token_url).mock(
        return_value=respx.MockResponse(200, json=_token_response())
    )
    db.add(
        PendingLogin(
            state="s1",
            next_path="//evil.example.com/pwn",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get(
            "/auth/callback",
            params={"code": "abc", "state": "s1"},
            cookies={"rehketo_oauth_state": "s1", "rehketo_oauth_verifier": "v1"},
        )
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://127.0.0.1:5173/")
    assert "evil.example.com" not in r.headers["location"]
```

- [ ] **Step 6: Run the rewritten test file**

Run (from `rehketo-api/`):
```bash
uv run pytest tests/integration/test_auth_next_preservation.py -v
```
Expected: PASS (login persists row; unsafe ignored; callback consumes row →
redirect; unsafe row → fallback). Needs Docker (testcontainers); start Docker
Desktop if the fixture errors with a connection refused.

- [ ] **Step 7: Lint + commit**

Run (from `rehketo-api/`):
```bash
uv run ruff format --check tests/integration/test_auth_next_preservation.py
uv run ruff check tests/integration/test_auth_next_preservation.py
```
(Run `uv run ruff format <file>` first if the check fails.) Then:
```bash
git add rehketo-api/tests/integration/test_auth_next_preservation.py
git commit -m "test: cover server-side pending-login next round-trip"
```

---

## Task 5: Full validation + PR + scan confirmation

**Files:** none (verification only).

- [ ] **Step 1: Run the api validation block** (from `rehketo-api/`):
```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
```
Expected: all pass. Quote the real `pytest` summary line.

- [ ] **Step 2: Repo-wide guards** (from repo root):
```bash
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

- [ ] **Step 3: Push and open the PR**:
```bash
git push -u origin fix/oauth-next-server-side
gh pr create --base main --fill
```

- [ ] **Step 4: After the PR CodeQL run completes, confirm no cookie-injection alert on the merge ref**:
```bash
gh api 'repos/iamrehket/rehketo/code-scanning/alerts?ref=refs/pull/<PR>/merge&per_page=100' \
  --jq '.[] | "#\(.number) \(.rule.id)"'
```
Expected: empty (note: PR runs are diff-informed; the authoritative confirmation
is the `push: main` scan after merge).

- [ ] **Step 5: After merge, confirm alert #7 is `fixed` on `main` with no replacement**:
```bash
gh api repos/iamrehket/rehketo/code-scanning/alerts/7 --jq '"#7 \(.rule.id): \(.state)"'
gh api 'repos/iamrehket/rehketo/code-scanning/alerts?state=open&per_page=100' --jq 'length'
```
Expected: `#7 ... fixed` and open-count `0`. **Not done until this holds**
(charter rule 5).

---

## Notes

- The verifier cookie staying in the browser is intentional and out of scope (a
  separate defense-in-depth follow-up). Only the flagged `next` is moved.
