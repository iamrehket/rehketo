# Automate the M1 manual validation: e2e specs for live-resume, kill/restart, and cancel

## Context

PR #41 (durable event bus, M1) has one unchecked test-plan item: the manual browser validation (two-tab live resume, kill-API-mid-stream recovery, cancel badge). The repo already has an offline e2e stack — pytest boots a real API + fake streaming Bifrost + built SPA, and drives the Playwright suite in `rehketo-ui/e2e/` with login solved via `devonly_login`. This plan extends that stack to cover all three scenarios automatically, on branch `feat/durable-event-bus`.

Discovered during planning: `cancel-run.e2e.ts` is currently `test.skip()`'d, so the cancel scenario has no automated coverage today — fixing it is folded in as item C. The permission chain was verified end-to-end and is NOT the cause (devonly roles `['User','Admin']` both grant `chat.cancel_run` in `rehketo/permissions/roles.py`; `+layout.ts` hydrates auth before render); the real cause is the ~1s visibility window of the `slow` profile racing button actionability, observed against the pre-durable-bus streaming stack.

**Out of scope (long-view note, user-requested record):** a dev-container local IdP (e.g. Authentik) to exercise real OIDC flows in dev/e2e instead of `devonly_login`. Belongs near the multi-IdP milestone in the roadmap.

**Key design findings (verified):**
- A SIGKILLed run leaves **no assistant message row** — the sweep only flips `runs.status` to `failed`. Post-restart, the conversation renders just the user message and `active_run_id` is null. So the restart spec asserts the sweep verdict via `GET /runs/{id}` → `status === 'failed'`, and the UI assertion is "clean reload, no hang, composer live" — not a failed-badge bubble. (Optional product follow-up, not in this plan: sweep persists a partial assistant message so reloads show a Failed badge.)
- The session `api_server` fixture is uvicorn-in-a-daemon-thread (unkillable) and shared by the whole Playwright suite → kill/restart needs its own **subprocess** API.
- Sharing the session DB would be doubly unsafe: function-scoped migrations could drop tables under the live session server, and the bus's LISTEN/NOTIFY would cross-talk between the two APIs. → dedicated database per chaos test, created in the same testcontainer.

## Work items

### Step 0 — Plan doc
Copy this plan into `docs/superpowers/plans/2026-06-10-e2e-validation-automation.md` and commit (repo convention).

### Step 1 — `marathon` profile in fake Bifrost
`rehketo-api/tests/e2e/fake_bifrost.py` — add to `_PROFILES` (and docstring list):
```python
"marathon": {
    # 40 × 250 ms = 10 s stream: long enough for a second tab to open
    # mid-stream (cold SPA open ≈ 3-5 s on CI) and for chaos kills; short
    # enough for a 60 s per-spec timeout.
    "chunks": tuple(f"tok{i} " for i in range(40)),
    "delay_s": 0.25,
    "title_fail": False,
},
```
`rehketo-ui/e2e/fixtures/auth.ts` — widen `setBifrostProfile`'s profile union with `'marathon'`. Distinct `tokN` words make partial-stream assertions unambiguous.

### Step 2 — Item A: two-tab live-resume spec
New `rehketo-ui/e2e/live-resume.e2e.ts` (same browser context → shared session cookie, no second login):
1. `test.setTimeout(60_000)`; set `marathon` profile; create chat; send message; capture conversation URL.
2. Anchor: `await expect(assistantBubble(page)).toContainText(/tok\d+/)` — stream provably live before tab2 opens (~9s window left).
3. `context.newPage()` → goto conversation URL → assert `/tok\d+/` appears **without sending anything** (proves `active_run_id` → reattach → replay).
4. Both tabs converge to the full text (assertions scoped to `assistantBubble` — the sidebar title also echoes the text after title-gen; timeout 20s).
5. Dedupe regression: `toHaveCount(1)` on `assistantBubble()` in both tabs (`div.justify-start` is unique to `AssistantBubble.svelte`).

### Step 3 — Item B prep: extract shared seams (edit > create; second call site exists now)
- `rehketo-api/tests/e2e/fixtures/api_server.py`: extract the env block (lines ~96-112) into a pure `e2e_env(*, port, db_url, bifrost_url, ui_dir) -> dict[str, str]`; session fixture consumes it via its monkeypatch loop. `_sa_url` and `_wait_http_200` get reused by the chaos fixture — export, don't duplicate.
- Extract the Playwright invocation from `test_browser_flows.py` into `rehketo-api/tests/e2e/playwright_runner.py`: `run_playwright(report_path, env_overrides, *cli_args)` (corepack check, idempotent chromium install, `pnpm exec playwright test --reporter=json [args]`, JSON-report walk, `pytest.fail` with per-spec failures). `test_browser_flows.py` shrinks to one call.

### Step 4 — Item B: chaos fixtures + pytest module
New `rehketo-api/tests/e2e/fixtures/chaos_api.py`:
- `chaos_db(_pg)` (function-scoped): `CREATE DATABASE chaos_<hex>` via `psycopg.connect(admin_dsn, autocommit=True)` (CREATE DATABASE can't run in a tx), `alembic upgrade head` against it (fresh DB — upgrade only), yield SQLAlchemy URL, teardown `DROP DATABASE ... WITH (FORCE)` (pg17).
- `chaos_api(chaos_db, fake_bifrost, ui_build, tmp_path)` (function-scoped): `spawn()` = `Popen([sys.executable, "-m", "uvicorn", "rehketo.main:app", "--host", "127.0.0.1", "--port", str(port), ...], env={**os.environ, **e2e_env(...)}, stdout/stderr → tmp log)`, block on healthz (60s — cold langchain import dominates). Plus a stdlib `ThreadingHTTPServer` on its own port: `POST /kill` → `proc.kill()` (SIGKILL = real crash, no lifespan shutdown, run stays `running` → exercises the sweep) + `wait`; `POST /restart` → `spawn()` on the **same port**, blocking until healthy. Yields `ChaosHandle(base_url, chaos_url)`; teardown shuts both down.
- Re-export both from `tests/e2e/conftest.py` (auto-`e2e`-marked like everything in the dir).

New `rehketo-api/tests/e2e/test_restart_recovery.py`: one test calling `run_playwright(report, {REHKETO_BASE_URL: chaos_api.base_url, REHKETO_BIFROST_URL, REHKETO_DEV_EMAIL, REHKETO_CHAOS_URL: chaos_api.chaos_url}, "restart-recovery.e2e.ts")` — filtered to just the chaos spec.

### Step 5 — Item B: restart Playwright spec
New `rehketo-ui/e2e/restart-recovery.e2e.ts`:
- `test.skip(!process.env.REHKETO_CHAOS_URL, ...)` — the main session suite matches every `*.e2e.ts` against the unkillable session server; this guard keeps it chaos-module-only (skips aren't failures in the report walk).
- `test.setTimeout(90_000)` (restart re-imports langchain mid-spec: 5-15s).
- Flow: `marathon` profile → create chat → capture `run_id` from the kickoff 202 via `page.waitForResponse` → anchor on `/tok\d+/` → `POST {chaos}/kill` → assert `getByRole('alert')` contains `/disconnected/i` (SSE retries 500+1000+2000ms exhaust against instant ECONNREFUSED) → `POST {chaos}/restart` (blocks until healthy; sweep runs in lifespan) → `GET /runs/{run_id}` → `status === 'failed'` (RunOut has no `error` field — assert status only) → reload conversation URL → user message visible, `assistantBubble` count 0, composer enabled (no reattach hang; `active_run_id` is null for failed runs).

### Step 6 — Item C: fix and un-skip cancel-run
`rehketo-ui/e2e/cancel-run.e2e.ts`:
- Replace the TODO with the verified diagnosis comment (permission chain sound; original failure was the ~1s `slow` window racing actionability, pre-durable-bus).
- `test.skip(` → `test(`; switch to `marathon`; `test.setTimeout(60_000)`.
- Anchor on `/tok\d+/` before asserting the Cancel button; click; assert `assistantBubble(page).getByText('Cancelled')` (scoped — unscoped `/cancelled/i` can match elsewhere); assert composer re-enabled.
- If it still fails, pull the Playwright trace (`retain-on-failure` already configured) before touching product code.

## Commits (conventional, stealth — no AI trailers)
1. `test(e2e): two-tab live-resume spec + marathon bifrost profile` (Steps 1-2)
2. `test(e2e): kill/restart chaos harness with dedicated database` (Steps 3-5)
3. `test(e2e): un-skip cancel-run with deterministic stream window` (Step 6)

## Verification
```bash
cd rehketo-api && uv run pytest -m e2e                       # full offline e2e (Docker required)
uv run pytest -m e2e tests/e2e/test_restart_recovery.py      # targeted while iterating
uv run pytest -m e2e tests/e2e/test_browser_flows.py
# Standard blocks:
uv run ruff format --check && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo && uv run lint-imports && uv run pytest && uv run python ../tools/check_contract.py
cd ../rehketo-ui && pnpm run lint && pnpm run check && pnpm run test:unit -- --run
cd .. && uv run --project rehketo-api python tools/agent_guards.py check && uv run --project rehketo-api python tools/sync_agent_rules.py --check
```
Run the chaos test twice in a row to shake out port-rebind or timing flakes.

## Risks / mitigations
- **Marathon lengthens the suite** ~10-20s per spec using it; `chat.e2e.ts` stays on `default`.
- **Port rebind after SIGKILL**: loopback sockets free on process death; `free_port()` uses SO_REUSEADDR. If rare `EADDRINUSE`, add a bind-retry in `spawn()`.
- **Global bifrost profile state**: every spec sets its own profile first; Playwright `workers: 1` + sequential pytest keep suites from overlapping.
- **Tab2 misses the stream on slow CI**: tab2 opens only after tab1's first delta (~9s remain); bump chunk count/delay in one place if needed.
