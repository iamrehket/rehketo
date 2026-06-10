"""Function-scoped subprocess api + kill/restart control for chaos e2e tests.

Why a subprocess: the session ``api_server`` is uvicorn-in-a-daemon-thread —
it can't be SIGKILLed without taking pytest down, and it's shared by the
whole Playwright suite. Crash testing needs a process we can really kill.

Why a dedicated database: function-scoped migrations against the shared
session DB would drop tables under the live session server, and the event
bus's LISTEN/NOTIFY would cross-talk between the two apis. Each chaos test
gets its own database inside the same testcontainer.

The chaos-control server is a stdlib HTTP server on its own port so the
Playwright spec (a separate process, driving a browser) can trigger
``POST /kill`` and ``POST /restart`` mid-test.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg import sql

from tests.e2e.fixtures.api_server import API_ROOT, _sa_url, _wait_http_200, e2e_env
from tests.e2e.fixtures.ports import free_port

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

    from testcontainers.postgres import PostgresContainer

    from tests.e2e.fixtures.bifrost_server import BifrostHandle


@dataclass(frozen=True)
class ChaosHandle:
    """Handle to the killable api: app base_url + chaos-control base_url."""

    base_url: str  # http://127.0.0.1:<api_port>
    chaos_url: str  # http://127.0.0.1:<control_port>  (POST /kill, /restart)


def _admin_dsn(sa_url: str) -> str:
    """psycopg wants a plain libpq DSN — strip SQLAlchemy's driver tag."""
    return sa_url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def chaos_db(_pg: PostgresContainer) -> Iterator[str]:
    """A throwaway database in the session container, migrated to head."""
    from alembic.config import Config

    from alembic import command
    from rehketo.config import get_settings

    admin_sa = _sa_url(_pg)
    name = f"chaos_{uuid.uuid4().hex[:8]}"
    # CREATE DATABASE can't run inside a transaction — autocommit required.
    with psycopg.connect(_admin_dsn(admin_sa), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    db_url = admin_sa.rsplit("/", 1)[0] + f"/{name}"

    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "alembic"))
    # alembic/env.py resolves the URL via get_settings(), NOT the Config's
    # sqlalchemy.url — point the env at the chaos DB for the upgrade. The
    # e2e_env placeholders satisfy Settings' other required fields so a
    # chaos-only run doesn't depend on a developer .env.
    #
    # The sub-second window while the env points at the chaos DB is observable
    # only by the session listener's reconnect loop (rehketo/runs/listen.py
    # re-resolves the DSN via get_settings()): a drop landing exactly then
    # would pin the listener to the chaos DB until DROP ... FORCE boots it and
    # the next reconnect self-heals. Root cause is alembic/env.py resolving via
    # get_settings() instead of Config's sqlalchemy.url — follow-up candidate,
    # out of scope here.
    mp = pytest.MonkeyPatch()
    try:
        placeholder_env = e2e_env(
            port=0, db_url=db_url, bifrost_url="http://127.0.0.1:0/v1", ui_dir=""
        )
        for key, value in placeholder_env.items():
            mp.setenv(key, value)
        get_settings.cache_clear()
        command.upgrade(cfg, "head")  # fresh DB — nothing to downgrade first
    finally:
        mp.undo()
        get_settings.cache_clear()
    try:
        yield db_url
    finally:
        with psycopg.connect(_admin_dsn(admin_sa), autocommit=True) as conn:
            # FORCE boots any connection the killed api left behind.
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def chaos_api(
    chaos_db: str,
    fake_bifrost: BifrostHandle,
    ui_build: pathlib.Path,
    tmp_path: pathlib.Path,
) -> Iterator[ChaosHandle]:
    port = free_port()
    # Computed ONCE so kill→restart reuses the same fernet key — the browser's
    # session cookie must stay valid across the crash.
    env = {
        **os.environ,
        **e2e_env(
            port=port,
            db_url=chaos_db,
            bifrost_url=fake_bifrost.base_url,
            ui_dir=str(ui_build),
        ),
    }
    log_path = tmp_path / "chaos-api.log"
    healthz = f"http://127.0.0.1:{port}/healthz"

    with log_path.open("w", encoding="utf-8") as log:

        def spawn() -> subprocess.Popen[bytes]:
            return subprocess.Popen(  # noqa: S603 -- fixed argv, test infra
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "rehketo.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=API_ROOT,
                env=env,
                stdout=log,
                stderr=log,
            )

        proc = spawn()
        try:
            # 60s: the cold langchain/deepagents import dominates first boot.
            _wait_http_200(healthz, timeout_s=60.0)
        except TimeoutError:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail(
                "chaos api subprocess never served /healthz; log tail:\n"
                + log_path.read_text(encoding="utf-8")[-2000:]
            )

        # The handler swaps in the restarted process through this box.
        procs = {"current": proc}
        # Serializes kill/spawn in handler threads vs teardown: a daemonic
        # handler thread mid-restart (between spawn and healthz) would otherwise
        # race teardown and orphan the freshly-spawned process.
        proc_lock = threading.Lock()

        class _ChaosHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path == "/kill":
                    with proc_lock:
                        # SIGKILL = real crash: no lifespan shutdown, so the
                        # run stays `running` in the DB and the restart's
                        # startup sweep has an orphan to fail.
                        procs["current"].kill()
                        procs["current"].wait(timeout=10)
                elif self.path == "/restart":
                    with proc_lock:
                        # Reap the old process before spawning so a
                        # double-restart doesn't leave the previous uvicorn
                        # squatting the port. kill()/wait() are no-ops on an
                        # already-dead process.
                        procs["current"].kill()
                        procs["current"].wait(timeout=10)
                        try:
                            procs["current"] = spawn()
                            # Reply only once healthz is green — uvicorn
                            # serves only after lifespan startup, so the
                            # startup sweep has run by then.
                            _wait_http_200(healthz, timeout_s=60.0)
                        except Exception as exc:
                            # Capture the subprocess log so a never-healthy
                            # respawn is debuggable from CI output rather than
                            # a bare socket error in the spec.
                            try:
                                log_tail = log_path.read_text(encoding="utf-8")[-2000:]
                            except Exception:
                                log_tail = "<unreadable>"
                            body = (
                                f'{{"ok": false, "error": {str(exc)!r}, '
                                f'"log": {log_tail!r}}}'
                            ).encode()
                            self.send_response(500)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                else:
                    self.send_error(404)
                    return
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                """Silence per-request stderr logging."""

        control = http.server.ThreadingHTTPServer(
            ("127.0.0.1", free_port()), _ChaosHandler
        )
        control_thread = threading.Thread(
            target=control.serve_forever, daemon=True, name="chaos-control"
        )
        control_thread.start()
        try:
            yield ChaosHandle(
                base_url=f"http://127.0.0.1:{port}",
                chaos_url=f"http://127.0.0.1:{control.server_address[1]}",
            )
        finally:
            control.shutdown()
            # server_close() releases the listening socket; shutdown() only
            # stops the serve_forever loop and leaves the socket open until GC.
            control.server_close()
            control_thread.join(timeout=10)
            with proc_lock:
                procs["current"].kill()
                procs["current"].wait(timeout=10)
