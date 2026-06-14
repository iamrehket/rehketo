from __future__ import annotations

import asyncio

from sqlalchemy import text

from rehketo.db import reset_engine_for_tests, sessionmaker
from rehketo.runs.claim import claim_next_run
from tests.integration._helpers import mk_running_run


async def _mk_queued_run() -> str:
    """A queued run reuses mk_running_run's seed, then resets status."""
    run_id = await mk_running_run()
    async with sessionmaker()() as db:
        await db.execute(
            text("UPDATE runs SET status='queued', started_at=NULL WHERE id=:r"),
            {"r": run_id},
        )
        await db.commit()
    return run_id


async def test_claim_marks_running_and_stamps_heartbeat(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    run_id = await _mk_queued_run()

    async with sessionmaker()() as db:
        claimed = await claim_next_run(db)

    assert claimed is not None
    assert str(claimed.id) == run_id

    async with sessionmaker()() as db:
        row = (
            await db.execute(
                text("SELECT status, heartbeat_at, started_at FROM runs WHERE id=:r"),
                {"r": run_id},
            )
        ).one()
    assert row.status == "running"
    assert row.heartbeat_at is not None
    assert row.started_at is not None


async def test_claim_returns_none_when_no_queued_runs(
    settings_env: object, db_url: str
) -> None:
    reset_engine_for_tests()
    async with sessionmaker()() as db:
        assert await claim_next_run(db) is None


async def test_two_claimers_get_disjoint_runs(
    settings_env: object, db_url: str
) -> None:
    """SKIP LOCKED: concurrent claimers never grab the same run."""
    reset_engine_for_tests()
    a = await _mk_queued_run()
    b = await _mk_queued_run()

    async def claim_one() -> str | None:
        async with sessionmaker()() as db:
            c = await claim_next_run(db)
        return None if c is None else str(c.id)

    r1, r2 = await asyncio.gather(claim_one(), claim_one())
    assert {r1, r2} == {a, b}  # disjoint, both claimed, none doubled
    assert r1 != r2
