"""Agent worker entry point.

    uv run python -m rehketo.cli.worker

Claims queued runs from postgres and drives the LangGraph agent loop, isolated
from the auth-holding API process. Mirrors rehketo.cli.serve's Windows event
loop policy handling (psycopg3 async needs SelectorEventLoop).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from rehketo.core.logging import get_logger
from rehketo.runs.event_bus import PostgresEventBus
from rehketo.runs.worker import run_worker

logger = get_logger(__name__)


async def _run() -> None:
    bus = PostgresEventBus()
    await bus.start()
    try:
        await run_worker(bus)
    finally:
        await bus.stop()


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
        )
    logger.info("rehketo agent worker starting")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
