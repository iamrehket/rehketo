"""Regression test: if the post-success title-generation step raises, the
SSE stream must still terminate with `run.ended`.

Today's `run_agent` (rehketo/agent/run.py) calls
`await generate_title_if_needed(conversation_id)` AFTER publishing
`run.status=succeeded` but BEFORE the success-branch's `run.ended`. The
title helper swallows its own exceptions, but if a regression let one
escape, control would fall into the outer `except Exception` — which
persists a *second* failed-state assistant message and republishes a
terminal event chain. That double-bookkeeping is confusing and fragile.

Phase C's run.py refactor consolidates the `run.ended` publish into a
single try/finally, making this test pass for the right reason
(guaranteed terminator) instead of the wrong one (the catch-all).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # fixture annotation

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import (
    FakeStreamingAgent,
    drain_sse,
    live_app,
    make_fake_build_agent,
    seed_user_and_conv,
)


@pytest.mark.asyncio
async def test_title_generation_failure_still_emits_run_ended(
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()
    import rehketo.agent.run as run_mod

    monkeypatch.setattr(
        run_mod,
        "build_agent",
        make_fake_build_agent(FakeStreamingAgent(("hel", "lo"))),
    )

    async def _boom(_cid: object) -> None:
        raise RuntimeError("title llm exploded")

    monkeypatch.setattr(run_mod, "generate_title_if_needed", _boom)

    _user, conv, sid, csrf = await seed_user_and_conv(db)
    cookies = {SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf}
    headers = {CSRF_HEADER: csrf}

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies=cookies,
            headers=headers,
            json={"content": "go"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        events = await drain_sse(c, run_id, cookies)

    types = [e["type"] for e in events]
    assert types, "SSE stream produced no events"
    assert types[-1] == "run.ended", (
        f"title failure broke the run.ended terminator; types={types}"
    )
    assert types.count("run.ended") == 1
