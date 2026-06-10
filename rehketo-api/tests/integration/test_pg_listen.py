from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.listen import listen


async def _notify(channel: str, payload: str) -> None:
    async with sessionmaker()() as db:
        await db.execute(
            text("SELECT pg_notify(:chan, :payload)"),
            {"chan": channel, "payload": payload},
        )
        await db.commit()


async def test_listen_dispatches_notify_payloads(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    received: asyncio.Queue[str] = asyncio.Queue()
    ready = asyncio.Event()
    task = asyncio.create_task(listen("test_chan", received.put_nowait, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await _notify("test_chan", "hello")
        assert await asyncio.wait_for(received.get(), timeout=10) == "hello"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)


async def test_listen_ignores_other_channels(settings_env: object, db_url: str) -> None:
    reset_engine_for_tests()
    received: asyncio.Queue[str] = asyncio.Queue()
    ready = asyncio.Event()
    task = asyncio.create_task(listen("chan_a", received.put_nowait, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        await _notify("chan_b", "wrong")
        await _notify("chan_a", "right")
        assert await asyncio.wait_for(received.get(), timeout=10) == "right"
        assert received.empty()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
