"""Session-scoped infrastructure for the offline E2E suite.

Re-exports the e2e fixtures so any test under ``tests/e2e/`` can request
them by name. The parent ``tests/conftest.py`` already provides ``_pg``
(session-scoped testcontainers postgres) — we depend on it transitively
via ``api_server``.

Auto-marks every test in this directory with ``@pytest.mark.e2e`` so the
default pytest run (which sets ``-m 'not live_deps and not e2e'``) skips
them. Opt in with ``uv run pytest -m e2e``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.e2e.fixtures.api_server import api_server  # noqa: F401  # re-export
from tests.e2e.fixtures.bifrost_server import fake_bifrost  # noqa: F401  # re-export
from tests.e2e.fixtures.chaos_api import (  # noqa: F401  # re-export
    chaos_api,
    chaos_db,
)
from tests.e2e.fixtures.ui_build import ui_build  # noqa: F401  # re-export

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="session")
def monkeypatch_session() -> Iterator[pytest.MonkeyPatch]:
    """Session-scoped MonkeyPatch — pytest's default is function-scoped."""
    mpatch = pytest.MonkeyPatch()
    try:
        yield mpatch
    finally:
        mpatch.undo()


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Auto-mark every test in tests/e2e/ as @pytest.mark.e2e."""
    e2e_marker = pytest.mark.e2e
    for item in items:
        if "tests/e2e/" in str(item.fspath) or "tests\\e2e\\" in str(item.fspath):
            item.add_marker(e2e_marker)
