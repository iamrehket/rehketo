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
"""

from __future__ import annotations

import base64
import pathlib
import secrets
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
    try:
        _wait_http_200(f"http://127.0.0.1:{port}/healthz")
        yield ApiHandle(port=port, base_url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        get_settings.cache_clear()
