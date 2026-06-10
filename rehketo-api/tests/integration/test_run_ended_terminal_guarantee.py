"""Regression test: the SSE stream's last event MUST always be `run.ended`,
on every terminal path (succeeded, failed, cancelled).

The UI's `subscribeRun` (`rehketo-ui/src/lib/sse.ts`) closes the EventSource
only when it sees `run.ended`. If any terminal path forgets to publish it,
the UI hangs forever showing a spinner. This test parametrizes the three
paths so any future refactor of `run_agent` (e.g., the try/finally
consolidation in plan Phase C) preserves the invariant.
"""

from __future__ import annotations

import asyncio

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


async def _no_title(_cid: object) -> None:
    return None


@pytest.mark.asyncio
async def test_success_path_ends_with_run_ended(
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
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

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
        f"success path did not terminate with run.ended; types={types}"
    )
    assert types.count("run.ended") == 1


@pytest.mark.asyncio
async def test_failure_path_ends_with_run_ended(
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()
    import rehketo.agent.run as run_mod

    # Agent yields one chunk then raises — exercises the failure branch.
    monkeypatch.setattr(
        run_mod,
        "build_agent",
        make_fake_build_agent(FakeStreamingAgent(("oh ",), raise_after_chunks=1)),
    )
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

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
    statuses = [e for e in events if e["type"] == "run.status"]
    assert any(e.get("status") == "failed" for e in statuses), (
        f"expected a run.status=failed event; got {statuses}"
    )
    assert types[-1] == "run.ended", (
        f"failure path did not terminate with run.ended; types={types}"
    )
    assert types.count("run.ended") == 1


@pytest.mark.asyncio
async def test_cancel_path_ends_with_run_ended(
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send a long-running agent, cancel mid-stream, verify run.ended last."""
    reset_registry_for_tests()
    import rehketo.agent.run as run_mod

    # 10 slow chunks gives us a window to send the cancel mid-stream.
    slow_chunks = tuple("abcdefghij")
    monkeypatch.setattr(
        run_mod,
        "build_agent",
        make_fake_build_agent(FakeStreamingAgent(slow_chunks, delay_s=0.1)),
    )
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

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

        # Start streaming in a task so we can cancel mid-flight.
        events: list[dict] = []

        async def _stream() -> None:
            collected = await drain_sse(c, run_id, cookies)
            events.extend(collected)

        stream_task = asyncio.create_task(_stream())

        # Wait until at least one message.delta has flowed, then cancel.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if any(e.get("type") == "message.delta" for e in events):
                break
        else:
            stream_task.cancel()
            raise AssertionError("no message.delta arrived before timeout")

        cancel_resp = await c.post(
            f"/runs/{run_id}/cancel",
            cookies=cookies,
            headers=headers,
        )
        # 204 = cancelled in-flight; 409 = already terminal (raced)
        assert cancel_resp.status_code in (204, 409), cancel_resp.text

        await asyncio.wait_for(stream_task, timeout=5.0)

    types = [e["type"] for e in events]
    assert types, "SSE stream produced no events"
    assert types[-1] == "run.ended", (
        f"cancel path did not terminate with run.ended; types={types}"
    )
    assert types.count("run.ended") == 1
