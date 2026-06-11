"""Regression test for the messages.created_at frozen-default bug.

Migration 0002 used `server_default="now()"` (a Python string), which
SQLAlchemy passed through verbatim as `DEFAULT 'now()'` — a quoted literal
Postgres implicitly cast to timestamptz at table-create time, FREEZING the
column default. Every INSERT got the same created_at; `ORDER BY created_at`
on identical values has no defined tie-break, and the planner happened to
surface assistant rows before user rows. Migration 0004 fixed the default
to `func.now()` so the per-INSERT timestamp is honored.

This test would have caught it before it shipped: POST several messages
with real time gaps, GET the conversation, assert the returned messages
alternate user/assistant in insertion order.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002  # fixture annotation

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.main import create_app
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import (
    FakeStreamingAgent,
    await_run_terminal,
    make_fake_build_agent,
    seed_user_and_conv,
)


@pytest.mark.asyncio
async def test_messages_returned_in_insertion_order(
    settings_env: pytest.MonkeyPatch,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three turns: user/assistant/user/assistant/user/assistant.

    Each user message has a distinct text and is sent ~50ms after the
    previous turn finishes. With migration 0004 in place, every insert
    gets a per-INSERT `now()`, so ORDER BY created_at returns chronological
    order. Without 0004, all six rows shared one frozen timestamp and the
    planner returned them grouped by role.
    """
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(
        run_mod,
        "build_agent",
        make_fake_build_agent(FakeStreamingAgent(("ack",))),
    )
    # Skip the (non-deterministic) title generation; it would just call the
    # real LLM stub and is irrelevant to message ordering.
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    _user, conv, sid, csrf = await seed_user_and_conv(db)
    cookies = {SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf}
    headers = {CSRF_HEADER: csrf}

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for body in ("first", "second", "third"):
            r = await c.post(
                f"/conversations/{conv.id}/messages",
                cookies=cookies,
                headers=headers,
                json={"content": body},
            )
            assert r.status_code == 202, r.text
            run_id = r.json()["run_id"]
            status = await await_run_terminal(c, run_id, cookies)
            assert status == "succeeded", f"unexpected status: {status}"
            # Real time between turns so the next user message's created_at
            # is strictly greater than the just-persisted assistant's.
            await asyncio.sleep(0.05)

        r = await c.get(f"/conversations/{conv.id}", cookies=cookies)
        assert r.status_code == 200, r.text

    messages = [i for i in r.json()["items"] if i["kind"] == "message"]
    roles = [m["role"] for m in messages]
    user_texts = [
        (m["content"].get("text") or "") for m in messages if m["role"] == "user"
    ]
    assistant_texts = [
        (m["content"].get("text") or "") for m in messages if m["role"] == "assistant"
    ]

    assert roles == ["user", "assistant"] * 3, (
        f"messages not alternating in time order; got roles={roles}"
    )
    assert user_texts == ["first", "second", "third"]
    assert all("ack" in t for t in assistant_texts)


async def _no_title(_cid: object) -> None:
    return None
