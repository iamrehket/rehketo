from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import rehketo.cli.worker as worker_cli

if TYPE_CHECKING:
    import pytest


async def test_main_starts_and_stops_cleanly(
    settings_env: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run wires bus + worker; cancelling it tears everything down without
    raising. We stub the bus and worker so the test needs no DB."""
    started: dict[str, bool] = {"bus": False, "worker": False}

    class FakeBus:
        async def start(self) -> None:
            started["bus"] = True

        async def stop(self) -> None:
            started["bus"] = False

    async def fake_run_worker(bus: Any, **_kw: Any) -> None:
        started["worker"] = True
        await asyncio.Event().wait()  # run forever until cancelled

    monkeypatch.setattr(worker_cli, "PostgresEventBus", lambda: FakeBus())
    monkeypatch.setattr(worker_cli, "run_worker", fake_run_worker)

    task = asyncio.create_task(worker_cli._run())
    await asyncio.sleep(0.05)
    assert started["bus"] and started["worker"]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert started["bus"] is False  # bus.stop() ran in the finally
