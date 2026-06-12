# Rehketo API — AGENTS.md

This is the canonical conventions document for anyone (human or agent) contributing to `rehketo-api`. Follow it. If a rule here conflicts with an ad-hoc instruction in a task prompt, this document wins unless the prompt explicitly calls out the override. The repo-wide North Star and charter live in the root `AGENTS.md`.

Reference docs:
- **Spec:** `docs/superpowers/specs/2026-04-19-chat-and-agent-v1-design.md`
- **Active plan:** `docs/superpowers/plans/2026-04-19-plan-1-api-foundation.md`

## What it is

The rehketo-api backend: FastAPI 3.14 + async SQLAlchemy/psycopg3, Alembic, deepagents + LangGraph runs streamed over SSE, Entra OIDC Pattern B auth, and an RBAC permission gate built to swap to OpenFGA. It is one half of the rehketo monorepo; the root `AGENTS.md` holds the project North Star.

## The charter

The repo-wide charter is in the root `AGENTS.md`. These api-specific rules have teeth:

1. **One permission gate.** Do not write a second permission check path. The only surface is `check_permission` (wrapped by `ResolvedPermissions.can/require`). (enforced: `check-single-permission-gate`)
2. **`resource_id` is always threaded.** Every permission call passes `resource_id` (even `None`) — the OpenFGA migration contract. (enforced: `check-permission-resource-id`)
3. **Settings only via `config.py`.** Never read `os.getenv`/`os.environ` outside `rehketo.config`. (enforced: `check-getenv-outside-config`)
4. **Logging via `get_logger(__name__)`.** Never `logging.getLogger` directly, never `print()`. (enforced: `check-logger-names`; ruff `T20`)
5. **No secret on disk.** Don't hold an Entra access token on disk or in a cache outside short-lived in-memory per-session caches. Refresh tokens are encrypted-at-rest only.
6. **Escape hatches carry a code.** `# type: ignore[code]` / `# noqa: CODE`; `# pragma: no cover` carries a reason. (enforced: `check-escape-hatches`)

## How to validate work

From `rehketo-api/`. Quote real output when you claim a step passed (charter rule 5).

```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports          # import-linter layer contract (.importlinter)
uv run pytest
uv run pytest -m e2e         # offline browser suite (fake Bifrost + Playwright); needs
                             # postgres up. Deselected by default, so it does NOT run
                             # with plain pytest — wire-shape changes must run it.
uv run python ../tools/check_contract.py   # OpenAPI snapshot vs. UI baseline
```

Repo guards also apply (run from the monorepo root): `python3 tools/agent_guards.py check`.

## Where things live

```
rehketo/
  main.py                    # app factory: create_app()
  config.py                  # Settings + get_settings()
  core/
    logging.py               # get_logger + sanitization filter (ALWAYS use this)
    ...                      # other cross-cutting utilities as needed
  db/
    __init__.py              # engine, sessionmaker, get_session dep
    models.py                # SQLAlchemy ORM models
  auth/
    crypto.py                # Fernet envelope encryption
    csrf.py                  # CSRF token issue/verify
    cookies.py               # cookie helpers + constants
    sessions.py              # session row lifecycle
    entra.py                 # MSAL / Entra OIDC flow
    dependencies.py          # resolve_session, AuthContext
    csrf_middleware.py       # CSRFMiddleware (wired in main.py)
  permissions/
    actions.py               # ACTIONS tuple + ACTIONS_SET
    roles.py                 # ROLE_PERMISSIONS dict (v1 RBAC)
    check.py                 # check_permission (single gate)
    dependencies.py          # ResolvedPermissions + resolve_permissions
  api/
    errors.py                # error envelope + exception handlers
    auth_routes.py           # /auth/*
    conversations.py         # /conversations/*
    me.py                    # /me, /me/capabilities
    ...                      # one module per resource domain
  cli/
    ...                      # admin/bootstrap scripts
tests/
  conftest.py                # fixtures: settings_env, db_url, db, ...
  unit/                      # pure functions; no DB, no network
  integration/               # real postgres (testcontainers); no mocked DB
```

**Rule:** each file has one clear responsibility. Don't stuff unrelated helpers in a module because it saves a file — split.

## Conventions in force

### Python version and typing

- Python 3.14. Use PEP 604 unions (`X | None`), PEP 585 generics (`list[int]`, `dict[str, int]`).
- `mypy` runs with `disallow_untyped_defs = true` and `check_untyped_defs = true`. **Every function has parameter and return annotations.** No exceptions.
- Prefer `from __future__ import annotations` at the top of modules where it simplifies forward references.
- `pydantic.mypy` plugin is active — use `pydantic.BaseModel` with typed fields for all request/response shapes.
- Use `SecretStr` for anything secret in `Settings`.

### Logging — ALWAYS use `rehketo.core.logging` (enforced: `check-logger-names`)

```python
from rehketo.core.logging import get_logger

logger = get_logger(__name__)

logger.info("session created for user_id=%s", user_id)
logger.warning("invalid csrf token from session_id=%s", session_id)
```

- **Never use `print()`.** Ruff `T20` blocks it.
- Never build log messages via f-string with unsanitized user input — pass args positionally; the sanitizing filter handles credentials and CR/LF stripping, but keep interpolation out of the message string to avoid injection and to keep log-aggregation-friendly formatting.
- For user-supplied values that need an extra guard, use `sanitize_for_log(value)` at the call site.
- For exceptions, prefer `format_exc_for_log(exc)` over `str(exc)` in log messages — it redacts the driver message.
- Attach `EndpointAccessFilter(["/healthz"])` to `uvicorn.access` if we want to silence access logs for noisy endpoints.

### Configuration (enforced: `check-getenv-outside-config`)

- All runtime knobs go through `rehketo.config.Settings`. Never read `os.getenv` directly outside `config.py`.
- Use `get_settings()` (lru_cached). Tests clear the cache via `get_settings.cache_clear()` in fixtures.
- Secrets use `SecretStr`; call `.get_secret_value()` at the point of use — never log the result.

### Database

- Async SQLAlchemy 2 + psycopg3. Driver URL: `postgresql+psycopg://...`.
- Get a session via the `get_session` FastAPI dependency (`rehketo.db.get_session`). Don't instantiate an engine in handlers.
- Commit at the end of a handler's unit of work. Don't hold transactions across request boundaries.
- Use ORM models for the common path; drop to `select(...)`/`update(...)` Core constructs when needed.
- For bulk or admin paths, `async with sessionmaker()() as s:` is fine.
- All models extend `rehketo.db.models.Base`. Put new tables there.

### Migrations (Alembic)

- Live in `alembic/versions/`. Revision ids use zero-padded numeric prefixes (`0001_...`, `0002_...`).
- Each revision has a complete `downgrade()`. Round-trip is verified (`alembic downgrade base && alembic upgrade head`) before commit.
- Do **not** autogenerate silently — always inspect the generated migration and trim unrelated changes.
- Extensions go at the top of `upgrade()` (`op.execute("CREATE EXTENSION IF NOT EXISTS citext")`).
- If a column type change needs application code coordination (e.g., `citext`), run the `ALTER` after the table is created.
- Schema order matters in migrations: create tables in dependency order (e.g., `runs` before `messages`, since `messages.run_id` FKs `runs`).

### FastAPI conventions

- One `APIRouter` per domain module. Include routers in `create_app()` via `app.include_router(...)`.
- Use `Depends(resolve_permissions)` on every endpoint that touches user data. The returned `ResolvedPermissions` object is the sole permission surface.
- Call `permissions.require("action.name", resource_type="...", resource_id=...)` or `permissions.can(...)` before touching data. **Always pass `resource_id`, even if v1 RBAC ignores it** — the signature is the OpenFGA migration contract.
- Use pydantic models for request and response shapes. Type the route's `response_model=`.
- Raise `HTTPException(status_code=..., detail=str)` for application errors. Let the error envelope middleware format the response.
- Never leak internal state (stack traces, driver messages, raw tokens) to the client. The `_unhandled_handler` returns a generic 500 body for uncaught exceptions.
- For 204 responses, return `Response(status_code=204)` or set `status_code=204` on the decorator.

### Permissions gate (enforced: `check-single-permission-gate`, `check-permission-resource-id`)

- **The ONLY permission surface is `check_permission(...)` (wrapped by `ResolvedPermissions.can/require`).** No endpoint reads roles directly. No agent tool reads roles directly.
- New actions must be added to `rehketo.permissions.actions.ACTIONS`. Dotted lowercase names (`chat.view_conversation`, `admin.manage_users`). Names must be `isidentifier()`-safe on each side of the dot.
- UI capabilities come from `GET /me/capabilities` — never reconstruct the list in the frontend.

### Sessions, cookies, CSRF (Pattern B)

- Session cookie: `httpOnly`, `Secure` (prod), `SameSite=Lax`. Opaque UUID stored in `sessions` table.
- Refresh tokens: encrypted at rest via `rehketo.auth.crypto.encrypt_token`. Never exposed outside the server.
- CSRF: double-submit cookie token issued via `issue_csrf_token(session_id)`, verified via `verify_csrf_token`. Enforcement by `CSRFMiddleware` on unsafe methods (POST/PUT/PATCH/DELETE). Exempt prefixes are defined in `rehketo.auth.csrf_middleware.CSRF_EXEMPT_PREFIXES`.
- Elevation: NOT in v1. When the first dangerous action lands, a second `session_elevated` cookie + `requires_elevation` dep arrives with it. Don't anticipate.

### Testing

- **TDD.** Write the failing test first, run it (must fail for the right reason), implement, run it (must pass), commit.
- **Integration tests hit real postgres via testcontainers.** Never mock the DB. The `db_url` / `db` fixtures in `conftest.py` are the contract.
- Unit tests are pure: no DB, no network, no filesystem except temp dirs.
- HTTP: use `httpx.AsyncClient(transport=ASGITransport(app=app), ...)` — no real network. For external HTTP (Entra token endpoint, etc.), use `respx` to mock.
- Per-file ruff overrides for tests: `T20`, `S101`, `PLR2004` relaxed. Tests can use `assert`, string literals, magic numbers.
- Tests should assert **behavior**, not implementation. Prefer asserting on observable state (HTTP response body, DB rows, SSE events) over mock-call assertions.

### Style and lint

Ruff is configured with: `F, E, W, I, B, UP, RUF, T20, SIM, TC, C90, PLR, S`. Relevant knobs:

- **Max complexity:** 12.
- **Max args:** 8. If you need more, pass a dataclass/BaseModel.
- **Max branches:** 10. **Max returns:** 6. **Max statements:** 50. If you're over, extract.
- **No `print()`.** Use logger.
- **No bare `assert` in app code** (bandit `S101`). OK in tests.
- Imports are sorted by ruff `I`. Let ruff format them.
- Ignored intentionally: `B008` (FastAPI `Depends()` defaults are the idiom), `RUF012` (SQLAlchemy `__table_args__`), `SIM112` (external env var names).

### Security (bandit)

- `bandit -r rehketo` runs on commit. Use the logger's redaction helpers for anything that might reach logs. Don't hand-roll crypto — use `cryptography` primitives. Use `secrets` module, not `random`, for tokens.

### Commits

- Conventional Commits. Subjects start with one of: `feat | fix | chore | docs | style | refactor | perf | test | build | ci | revert`. A scope is optional in parentheses: `feat(auth): ...`.
- One logical change per commit. TDD commits are fine (test + impl together); don't mix unrelated changes.
- **No AI attribution.** No `Co-Authored-By: Claude`, no generated-with trailers. Stealth mode. (enforced: `check-no-ai-attribution`)
- Clean message body; optional bullets only if the subject doesn't explain the why.

### Pre-commit

Pre-commit runs from the monorepo root (`.pre-commit-config.yaml`). API hooks (scoped to `rehketo-api/`): `ruff check`, `mypy rehketo`, `bandit -r rehketo`, `lint-imports`. Plus repo-wide guards (`agent_guards check`, mirror sync) and commit-msg checks (`conventional-pre-commit`, `no-ai-attribution`). **All must pass before you can commit.** Install once at the root: `pre-commit install --hook-type pre-commit --hook-type commit-msg`. If `ruff` finds issues, run `uv run ruff check --fix rehketo` first. If `mypy` flags anything, fix it — don't suppress with `# type: ignore` except for genuinely intractable cases, and when you do, include a specific code (`# type: ignore[code]`).

### Error envelope

All API errors (4xx, 5xx) return:
```json
{"error": {"code": "kebab_or_snake", "message": "human-readable"}}
```
Common codes: `bad_request`, `unauthenticated`, `forbidden`, `not_found`, `conflict`, `validation_failed`, `internal_error`. Extend the map in `rehketo.api.errors.ERROR_CODE_BY_STATUS` when adding a new status.

### SSE

Event shape is our stable schema (not a passthrough of upstream Responses events), every event carries a per-run monotonic `sequence`, and subscribers reconnect with `?from_sequence=N`. The stream closes on `run.ended` only — `run.status` alone (e.g. `succeeded`) is not a terminator, since title generation may emit `conversation.updated` afterward.

### Architectural constraints (import-linter) (enforced: `lint-imports`)

Authored in `rehketo-api/.importlinter`. The contracts:

- `rehketo.api.*` MAY depend on `rehketo.auth`, `rehketo.permissions`, `rehketo.db`, `rehketo.core`, `rehketo.config`.
- `rehketo.auth`, `rehketo.permissions` MUST NOT depend on `rehketo.api`.
- `rehketo.db.models` MUST NOT depend on anything outside `rehketo.db` and stdlib + SQLAlchemy.
- `rehketo.core.logging` MUST NOT depend on anything in the `rehketo` package (it's foundational).
- Cross-domain direct imports (e.g., `rehketo.permissions.check` importing from `rehketo.auth.sessions`) are smells — route through dependencies/interfaces.

### FastAPI + Pydantic typing — common gotchas

Mypy with `disallow_untyped_defs = true` and the pydantic plugin is strict. Get these right the first time — they are where the lint cycle burns the most iterations.

**1. Prefer `Annotated[T, Depends(...)]` over `T = Depends(...)`.**
The old `param: T = Depends(provider)` form makes mypy warn about default-value type mismatch. Use:
```python
from typing import Annotated
from fastapi import Depends

async def handler(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> ConversationOut: ...
```
Same pattern for `Query`, `Path`, `Body`, `Header`, `Cookie`, `Form`, `File`:
```python
conversation_id: Annotated[UUID, Path(...)],
include_archived: Annotated[bool, Query(default=False)] = False,
rehketo_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
```

**2. Pydantic v2 API — use the v2 names.**
- `model_config = ConfigDict(...)` — NOT an inner `class Config:` (that's v1).
- `model.model_dump()` and `model.model_dump_json()` — NOT `.dict()` / `.json()`.
- `@field_validator("name")` / `@model_validator(mode="after")` — NOT `@validator` / `@root_validator`.
- `model_fields`, `model_rebuild()`, `model_validate()` — v2 names.
- Deprecated constraint shortcuts (`conlist`, `constr`, `conint`, ...) — use `Annotated[list[X], Field(max_length=...)]` etc. instead.

**3. `SecretStr` — `.get_secret_value()` at the boundary.**
`Settings` secrets typed as `SecretStr` cannot be passed where `str` is expected. Call `.get_secret_value()` at the exact use site (so it doesn't leak into logs via f-strings earlier up the call chain):
```python
client_secret = settings.entra_client_secret.get_secret_value()
```

**4. PEP 604 unions everywhere.**
`str | None`, not `Optional[str]`. `int | float`, not `Union[int, float]`. Ruff `UP` enforces.

**5. Route return types must match `response_model`.**
If you declare `response_model=ConversationOut`, annotate the handler as `-> ConversationOut`. Don't annotate as `dict` and rely on FastAPI coercion — mypy can't see through that.

```python
@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation(...) -> ConversationOut:
    ...
    return ConversationOut(id=conv.id)   # return the model, not a dict
```

For 204 / empty responses, return `Response(status_code=204)` and annotate the handler as `-> Response`. Do not set `response_model` on 204 endpoints.

**6. Dependency functions are typed too.**
```python
async def get_session() -> AsyncIterator[AsyncSession]: ...
async def resolve_session(...) -> AuthContext: ...
async def resolve_permissions(...) -> ResolvedPermissions: ...
```
Typed dependency return values flow into handlers through `Annotated[T, Depends(...)]` and mypy knows the exact type.

**7. Async generators vs async iterators.**
Use `AsyncIterator[T]` for dependency yields (simple yield-then-cleanup), `AsyncGenerator[T, None]` when you need `send()`/`throw()`. For SSE endpoints, `AsyncIterator[str]` or `AsyncIterator[bytes]`.

**8. Response subclass return types.**
If you return a `RedirectResponse` or `JSONResponse` from a handler, annotate as `-> Response` (the base) and do NOT set a `response_model`. Mixing a custom `Response` return with a `response_model` is ambiguous; pick one.

**9. Pydantic `Field` positional-vs-keyword.**
In v2, the first positional arg to `Field` is `default`. `Field("", description="...")` sets default to `""`. To require the field, omit the default or use `Field(..., description="...")` (the literal `...` ellipsis is the v2-accepted required sentinel).

**10. Don't use `List[X]` / `Dict[K, V]` / `Optional[X]`.**
PEP 585/604: `list[X]`, `dict[K, V]`, `X | None`. Ruff `UP006` / `UP007` will flag.

**11. `BaseSettings` env var parsing.**
Booleans: `pydantic-settings` reads `"1"`, `"true"`, `"yes"`, `"on"` (case-insensitive) as `True`. Lists: comma-separated string in env (`CORS_ORIGINS=http://a,http://b`) → `list[str]`. JSON-structured values need explicit parsing.

**12. `model_validate` vs construction.**
To instantiate a pydantic model from a dict (e.g., after decoding JSON), use `Model.model_validate(d)`. Direct `Model(**d)` also works but skips field aliasing and sometimes coercion paths.

**13. Forward references with SQLAlchemy.**
Don't import pydantic models into `rehketo.db.models` for response-shaping. Define pydantic response models next to the routes that use them (`rehketo.api.conversations.ConversationOut`), so database and API layers stay separable.

## Don'ts

- Do not raise non-HTTPException errors from handlers and expect clean responses — wrap them.
- Do not add a feature flag, config knob, or code path for "later." YAGNI.
- Do not add backwards-compatibility shims during v1. If behavior changes, change it.
- Do not write comments that describe what obvious code does. Comment only the non-obvious why.
