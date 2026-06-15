_default:
    @just --list

# Bring up postgres + bifrost (detached).
[working-directory("rehketo-api/deploy")]
db:
    @test -f .env || { echo "rehketo-api/deploy/.env missing — cp .env.example .env"; exit 1; }
    docker compose up -d postgres bifrost

# Stop postgres + bifrost.
[working-directory("rehketo-api/deploy")]
db-down:
    docker compose down

# alembic reads DATABASE_URL from rehketo-api/.env; run after pulling a migration.
# Apply pending DB migrations (run before `just api`).
[working-directory("rehketo-api")]
migrate:
    @test -f .env || { echo "rehketo-api/.env missing — cp .env.example .env"; exit 1; }
    uv run alembic upgrade head

# Run the FastAPI backend on :8000 (foreground).
[working-directory("rehketo-api")]
api:
    @test -f .env || { echo "rehketo-api/.env missing — cp .env.example .env"; exit 1; }
    uv run python -m rehketo.cli.serve

# Run the agent worker (foreground). Claims and executes queued runs.
[working-directory("rehketo-api")]
worker:
    @test -f .env || { echo "rehketo-api/.env missing — cp .env.example .env"; exit 1; }
    uv run python -m rehketo.cli.worker

# Run api + worker together (dev convenience; prod runs them as separate services).
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT
    just api &
    just worker &
    wait

# Run the SvelteKit UI on :5173 (foreground).
[working-directory("rehketo-ui")]
ui:
    pnpm dev

# A freshly published version isn't pulled in for a day (uv `exclude-newer`,
# pnpm `minimumReleaseAge`).
# Update backend + UI dependencies (honoring the 1-day cooldown).
update-deps: update-deps-api update-deps-ui

# Upgrade the api lockfile to the newest allowed versions (older than 1 day).
[working-directory("rehketo-api")]
update-deps-api:
    uv lock --upgrade

# Upgrade UI dependencies to latest (older than 1 day) and refresh the lockfile.
[working-directory("rehketo-ui")]
update-deps-ui:
    pnpm update --latest

# Run the backend + UI test runners (default markers; no e2e/live_deps).
test: test-api test-ui

# Run the backend pytest suite (skips e2e/live_deps per pyproject addopts).
[working-directory("rehketo-api")]
test-api:
    uv run pytest

# Run the UI unit tests once (vitest, non-watch).
[working-directory("rehketo-ui")]
test-ui:
    pnpm run test:unit -- --run
