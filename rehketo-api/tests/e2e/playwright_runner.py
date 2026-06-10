"""Invoke the rehketo-ui Playwright suite as a subprocess from pytest.

The Python fixtures (`api_server`/`chaos_api`, `fake_bifrost`, `ui_build`)
spin up the full backend on real ports. `run_playwright` plumbs the
dynamically-allocated URLs in via env vars, runs Playwright with the JSON
reporter, and parses the report to surface per-spec failures back to pytest.

Browser install: `corepack pnpm exec playwright install chromium` is
idempotent (no-op if already present). We run it on every call so the
suite is self-contained — first run downloads ~100MB, subsequent runs
are cached.
"""

# ruff: noqa: S607 -- shells out to corepack/pnpm via user PATH (test infra)

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

# parents[0]=e2e, [1]=tests, [2]=rehketo-api, [3]=repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
UI_ROOT = REPO_ROOT / "rehketo-ui"


def _ensure_corepack() -> None:
    if shutil.which("corepack") is None:
        pytest.skip("corepack not on PATH (Node not installed?)")


def _walk_failures(suite: dict[str, Any], out: list[dict[str, Any]]) -> None:
    for spec in suite.get("specs", []):
        for t in spec.get("tests", []):
            for r in t.get("results", []):
                if r.get("status") in {"failed", "timedOut", "interrupted"}:
                    out.append(
                        {
                            "title": spec.get("title", "?"),
                            "file": spec.get("file", "?"),
                            "status": r["status"],
                            "error": (r.get("error") or {}).get("message", ""),
                        }
                    )
    for child in suite.get("suites", []):
        _walk_failures(child, out)


def run_playwright(
    report_path: pathlib.Path,
    env_overrides: dict[str, str],
    *cli_args: str,
) -> None:
    """Run `playwright test [cli_args]` in rehketo-ui; fail pytest on failures."""
    _ensure_corepack()

    # Install Chromium (idempotent — skips download if already cached).
    install = subprocess.run(
        ["corepack", "pnpm", "exec", "playwright", "install", "chromium"],
        cwd=UI_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if install.returncode != 0:
        pytest.skip("playwright install chromium failed:\n" + install.stderr[-500:])

    env = {
        **os.environ,
        **env_overrides,
        # Tell Playwright's json reporter where to write the report (vs stdout).
        "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path),
    }

    result = subprocess.run(  # noqa: S603 -- cli_args come from our own tests
        [
            "corepack",
            "pnpm",
            "exec",
            "playwright",
            "test",
            "--reporter=json",
            *cli_args,
        ],
        cwd=UI_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    if not report_path.exists():
        pytest.fail(
            "Playwright did not produce a JSON report.\n"
            f"exit={result.returncode}\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )

    report = json.loads(report_path.read_text())
    failures: list[dict[str, Any]] = []
    for s in report.get("suites", []):
        _walk_failures(s, failures)

    # Genuine failures take precedence — check them first.
    if failures:
        lines = [
            f"  - [{f['file']}] {f['title']} ({f['status']})\n      {f['error'][:400]}"
            for f in failures
        ]
        pytest.fail(
            f"Playwright reported {len(failures)} failed test(s):\n"
            + "\n".join(lines)
            + f"\n\nFull stdout:\n{result.stdout[-2000:]}"
        )

    # Refuse to pass when nothing actually ran — a filter or skip-guard drift can
    # silently disarm a suite and produce a vacuous green.
    stats = report.get("stats", {})
    if stats.get("expected", 0) == 0 and not failures:
        pytest.fail(
            "Playwright ran zero expected tests "
            f"(stats={stats!r}) — a filter or skip-guard drift can silently "
            "disarm a suite; refusing to pass vacuously.\n"
            f"stdout:\n{result.stdout[-2000:]}"
        )
