"""Drive the rehketo-ui Playwright suite against the live api + fake Bifrost.

The Phase B Python fixtures (`api_server`, `fake_bifrost`, `ui_build`) spin
up the full backend on real ports; `run_playwright` handles the subprocess
invocation, env plumbing, and JSON-report failure parsing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.e2e.playwright_runner import run_playwright

if TYPE_CHECKING:
    import pathlib

    from tests.e2e.fixtures.api_server import ApiHandle
    from tests.e2e.fixtures.bifrost_server import BifrostHandle


def test_playwright_browser_flows(
    api_server: ApiHandle,
    fake_bifrost: BifrostHandle,
    tmp_path: pathlib.Path,
) -> None:
    run_playwright(
        tmp_path / "playwright-report.json",
        {
            "REHKETO_BASE_URL": api_server.base_url,
            "REHKETO_BIFROST_URL": fake_bifrost.base_url,
            "REHKETO_DEV_EMAIL": "playwright@example.com",
        },
    )
