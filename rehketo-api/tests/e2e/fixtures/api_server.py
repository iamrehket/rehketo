"""Session-scoped uvicorn-in-thread fixture for the api with same-origin UI.

The fixture:
- Reuses the session-scoped `_pg` (testcontainers postgres) from
  tests/conftest.py so we don't pay the postgres startup cost twice.
- Wires `UI_STATIC_DIR` to the freshly-built UI bundle so the api serves
  the SPA on `/`. Browser + Playwright see UI and API on a single origin —
  no `PUBLIC_API_BASE`, no Vite proxy, no CORS.
- Wires `BIFROST_BASE_URL` to the fake bifrost on its allocated port.
- Runs alembic upgrade head before booting so a clean schema is in place.
- Boots uvicorn in a daemon thread on its own asyncio loop. We don't run
  inside pytest-asyncio's loop because the api uses lifespan="on" (which
  pytest-asyncio's loop fights about) and a real port is required so
  Playwright can hit it.
- Spawns a worker subprocess (python -m rehketo.cli.worker) that claims
  and executes queued runs. A subprocess is used (not a thread) to avoid
  "attached to a different loop" errors from the shared SQLAlchemy async
  engine singleton.
"""

from __future__ import annotations

import base64
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import uvicorn

from tests.e2e.fixtures.ports import free_port

if TYPE_CHECKING:
    from collections.abc import Iterator

    from testcontainers.postgres import PostgresContainer

    from tests.e2e.fixtures.bifrost_server import BifrostHandle


# parents[0]=fixtures, [1]=e2e, [2]=tests, [3]=rehketo-api root
API_ROOT = pathlib.Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ApiHandle:
    """Handle to the running api: port + base_url for client requests."""

    port: int
    base_url: str  # http://127.0.0.1:<port>


def _sa_url(pg: PostgresContainer) -> str:
    """Same conversion as tests/conftest.py::_sa_url — psycopg + 127.0.0.1."""
    raw = pg.get_connection_url()
    if "+psycopg2" in raw:
        raw = raw.replace("+psycopg2", "+psycopg")
    if raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw.replace("@localhost:", "@127.0.0.1:")


def e2e_env(*, port: int, db_url: str, bifrost_url: str, ui_dir: str) -> dict[str, str]:
    """Every env var the api needs for an e2e boot.

    Shared by the session uvicorn-in-thread fixture below and the chaos
    subprocess fixture (tests/e2e/fixtures/chaos_api.py) so the two stacks
    can't drift. A fresh fernet key is generated per call — each consumer
    is its own isolated session world.
    """
    fernet_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return {
        "APP_ENV": "e2e",
        "DATABASE_URL": db_url,
        "SESSION_ENCRYPTION_KEY": fernet_key,
        "CSRF_SIGNING_KEY": "x" * 64,
        "ENTRA_TENANT_ID": "tid",
        "ENTRA_CLIENT_ID": "cid",
        "ENTRA_CLIENT_SECRET": "secret",
        "ENTRA_REDIRECT_URI": f"http://127.0.0.1:{port}/auth/callback",
        "UI_POST_LOGIN_URL": "/",
        "DEVONLY_LOGIN_ENABLED": "true",
        "BIFROST_BASE_URL": bifrost_url,
        "BIFROST_API_KEY": "test-key",
        "AGENT_MODEL": "claude-sonnet-4-6",
        "COOKIE_SECURE": "false",
        "UI_STATIC_DIR": ui_dir,
    }


def _wait_http_200(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:  # noqa: S310 -- hardcoded 127.0.0.1 test health endpoint
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
        time.sleep(0.1)
    raise TimeoutError(
        f"{url} did not return 200 within {timeout_s}s; last={last_err!r}"
    )


@pytest.fixture(scope="session")
def api_server(
    monkeypatch_session: pytest.MonkeyPatch,
    _pg: PostgresContainer,
    fake_bifrost: BifrostHandle,
    ui_build: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ApiHandle]:
    from alembic.config import Config

    from alembic import command

    port = free_port()
    db_url = _sa_url(_pg)

    # Env must be set BEFORE importing rehketo.main (Settings cache, db engine).
    env = e2e_env(
        port=port,
        db_url=db_url,
        bifrost_url=fake_bifrost.base_url,
        ui_dir=str(ui_build),
    )
    for key, value in env.items():
        monkeypatch_session.setenv(key, value)

    from rehketo.config import get_settings

    get_settings.cache_clear()

    # Fresh schema for the session.
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    from rehketo.main import create_app

    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="api-server")
    thread.start()

    worker_proc: subprocess.Popen[bytes] | None = None
    tmp_dir = tmp_path_factory.mktemp("api_server")
    worker_log_path = tmp_dir / "worker.log"

    try:
        _wait_http_200(f"http://127.0.0.1:{port}/healthz")

        # Spawn the agent worker so queued runs get claimed and executed.
        # A subprocess (not a thread) avoids "attached to a different loop"
        # errors from the shared SQLAlchemy async engine singleton.
        with worker_log_path.open("w", encoding="utf-8") as worker_log:
            worker_proc = subprocess.Popen(
                [sys.executable, "-m", "rehketo.cli.worker"],
                cwd=API_ROOT,
                env={**os.environ, **env},
                stdout=worker_log,
                stderr=worker_log,
            )

        # Give the worker a moment to start its claim loop, then verify it
        # didn't exit immediately (e.g. import error or bad config).
        time.sleep(1.0)
        if worker_proc.poll() is not None:
            log_tail = worker_log_path.read_text(encoding="utf-8")[-2000:]
            rc = worker_proc.returncode
            pytest.fail(
                f"agent worker subprocess exited immediately (rc={rc});"
                f" log tail:\n{log_tail}"
            )

        yield ApiHandle(port=port, base_url=f"http://127.0.0.1:{port}")
    finally:
        # Terminate the worker before the API so it can drain cleanly.
        if worker_proc is not None:
            worker_proc.terminate()
            try:
                worker_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_proc.kill()
                worker_proc.wait()
        server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()
