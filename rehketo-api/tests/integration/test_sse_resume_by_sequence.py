"""Integration test — SSE resume by sequence number.

Verifies that a client can reconnect to ``GET /runs/{id}/events?from_sequence=N``
after consuming some events and receive only events with ``sequence >= N``,
with no duplicate events.

Strategy: same fake-agent approach as ``test_run_agent_end_to_end.py``.  The
fake agent yields many chunks so the run produces enough events to reliably
consume 3 before disconnecting and reconnecting.  A small ``asyncio.sleep``
between chunks keeps the run alive long enough for the reconnect.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

try:
    from langchain_core.messages import AIMessageChunk
except ImportError:  # pragma: no cover
    AIMessageChunk = None  # type: ignore[assignment,misc]


_NUM_CHUNKS = 10  # enough to reliably consume 3 before stream ends


class _SlowAgent:
    """Fake agent that streams many chunks with a small delay between each."""

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        for i in range(_NUM_CHUNKS):
            await asyncio.sleep(0.02)  # 20 ms gap — keeps run alive for reconnect
            chunk = AIMessageChunk(content=f"tok{i}", id="msg-slow-1")
            yield (chunk, {"langgraph_node": "agent"})


async def _fake_build_agent(
    run_id: str, system_prompt: str
) -> AsyncIterator[_SlowAgent]:
    yield _SlowAgent()


async def _collect_lines(
    client: AsyncClient,
    url: str,
    cookies: dict[str, str],
    limit: int | None = None,
    query: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Open an SSE stream, collect up to ``limit`` data events, then close."""
    collected: list[dict[str, Any]] = []
    qs = "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
    full_url = f"{url}?{qs}" if qs else url
    async with client.stream("GET", full_url, cookies=cookies) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                collected.append(json.loads(line[6:]))
                if limit is not None and len(collected) >= limit:
                    break
    return collected


@pytest.mark.asyncio
async def test_sse_resume_by_sequence(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db.add_all([UserRole(user_id=u.id, role="User"), conv])  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    sid = await create_session(
        db,  # type: ignore[arg-type]
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))
    cookies = {SESSION_COOKIE: str(sid)}

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "stream many tokens"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        sse_url = f"/runs/{run_id}/events"

        # First connection: consume exactly 3 events then disconnect.
        first_batch = await _collect_lines(c, sse_url, cookies, limit=3)
        assert len(first_batch) == 3

        # The 3rd event consumed has sequence = first_batch[2]["sequence"].
        # Reconnect from that sequence + 1 to get the rest without duplicates.
        resume_seq = first_batch[-1]["sequence"] + 1

        # Second connection: reconnect with from_sequence=resume_seq.
        resumed = await _collect_lines(
            c, sse_url, cookies, query={"from_sequence": str(resume_seq)}
        )

    # All resumed events must have sequence >= resume_seq (no dups).
    assert len(resumed) > 0
    for event in resumed:
        assert event["sequence"] >= resume_seq, (
            f"Got duplicate/early event with sequence {event['sequence']}, "
            f"expected >= {resume_seq}"
        )
