from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import update

from rehketo.agent.run import run_agent
from rehketo.core.logging import get_logger
from rehketo.db import sessionmaker
from rehketo.db.models import Run
from rehketo.runs.cancellation import RunControlListener
from rehketo.runs.claim import RUN_QUEUED_CHANNEL, ClaimedRun, claim_next_run
from rehketo.runs.heartbeat import HEARTBEAT_INTERVAL_SECONDS, beat
from rehketo.runs.listen import listen
from rehketo.runs.reaper import run_reaper
from rehketo.runs.registry import RunTaskRegistry

if TYPE_CHECKING:
    from uuid import UUID

    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

DEFAULT_CONCURRENCY = 4
DEFAULT_POLL_INTERVAL = 2.0


async def run_worker(
    bus: RunEventBus,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Claim and execute runs forever. Owns a control listener (cross-process
    cancel), a run_queued doorbell listener, and the reaper. Runs until
    cancelled; intended as the worker process's top-level coroutine."""
    registry = RunTaskRegistry()
    control = RunControlListener(registry)
    await control.start()

    wake = asyncio.Event()
    doorbell = asyncio.create_task(
        listen(RUN_QUEUED_CHANNEL, lambda _payload: wake.set())
    )
    reaper = asyncio.create_task(run_reaper(bus))
    active: set[asyncio.Task[None]] = set()

    try:
        while True:
            # Fill open slots, claiming until the queue is dry or we're full.
            while len(active) < concurrency:
                async with sessionmaker()() as db:
                    claimed = await claim_next_run(db)
                if claimed is None:
                    break
                task = asyncio.create_task(_supervise(claimed, bus, registry))
                active.add(task)
                task.add_done_callback(active.discard)
            # Sleep until a doorbell or the poll floor, whichever first.
            wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=poll_interval)
    finally:
        doorbell.cancel()
        reaper.cancel()
        for task in (doorbell, reaper):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in list(active):
            task.cancel()
        for task in list(active):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await control.stop()


async def _supervise(
    claimed: ClaimedRun, bus: RunEventBus, registry: RunTaskRegistry
) -> None:
    """Drive one claimed run: short-circuit a pre-cancelled run, else run the
    agent under a heartbeat that doubles as the lost-NOTIFY cancel backstop."""
    if claimed.cancel_requested_at is not None:
        await _finalize_precancelled(claimed.id, bus)
        return

    run_task = asyncio.create_task(run_agent(claimed.id, bus))
    registry.register(claimed.id, run_task)
    heart = asyncio.create_task(_heartbeat(claimed.id, run_task))
    try:
        await run_task
    except asyncio.CancelledError:
        # Worker shutdown: cancel the run too so it doesn't leak unsupervised.
        # run_agent's shielded CancelledError finalizer marks it cancelled.
        run_task.cancel()
        with contextlib.suppress(BaseException):
            await run_task
        raise
    except Exception:
        logger.exception("run %s failed in supervisor", claimed.id)
    finally:
        heart.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heart


async def _heartbeat(run_id: UUID, run_task: asyncio.Task[None]) -> None:
    """Stamp heartbeat_at on a fixed cadence (the reaper's liveness signal) and
    poll cancel_requested_at as a backstop for a control NOTIFY lost during a
    listener reconnect. Cancels run_task if a cancel is pending. Survives
    transient DB errors so a blip doesn't stop the heartbeat and get the run
    false-reaped; runs until cancelled by _supervise's finally."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        try:
            cancel_requested = await beat(run_id)
        except Exception:
            logger.exception("heartbeat failed for run %s", run_id)
            continue
        if cancel_requested:
            run_task.cancel()
            return


async def _finalize_precancelled(run_id: UUID, bus: RunEventBus) -> None:
    """A run cancelled while parked (queued/pending_approval) carries
    cancel_requested_at at claim time. Finalize 'cancelled' without invoking
    the graph, keeping all finalization on the worker side."""
    async with sessionmaker()() as db:
        await db.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="cancelled", finished_at=datetime.now(UTC))
        )
        await db.commit()
    with contextlib.suppress(Exception):
        await bus.publish(str(run_id), {"type": "run.status", "status": "cancelled"})
    # Guaranteed terminator — isolated so a failed status publish can't strand it.
    with contextlib.suppress(Exception):
        await bus.publish(str(run_id), {"type": "run.ended"})
