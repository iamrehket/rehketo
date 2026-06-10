"""Kill/restart chaos: SIGKILL the api mid-stream, restart, assert recovery.

Runs only the restart-recovery spec against the function-scoped subprocess
api (``chaos_api``) — the session ``api_server`` is unkillable (in-thread)
and shared, so the main browser-flow suite never sets REHKETO_CHAOS_URL
and the spec self-skips there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.playwright_runner import run_playwright

if TYPE_CHECKING:
    import pathlib

    from tests.e2e.fixtures.bifrost_server import BifrostHandle
    from tests.e2e.fixtures.chaos_api import ChaosHandle


def test_playwright_restart_recovery(
    chaos_api: ChaosHandle,
    fake_bifrost: BifrostHandle,
    tmp_path: pathlib.Path,
) -> None:
    run_playwright(
        tmp_path / "playwright-chaos-report.json",
        {
            "REHKETO_BASE_URL": chaos_api.base_url,
            "REHKETO_BIFROST_URL": fake_bifrost.base_url,
            "REHKETO_DEV_EMAIL": "chaos@example.com",
            "REHKETO_CHAOS_URL": chaos_api.chaos_url,
        },
        "restart-recovery.e2e.ts",
    )
