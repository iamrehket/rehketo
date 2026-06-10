"""Integration test — full agent run end-to-end with fake streaming.

Strategy: monkey-patch ``rehketo.agent.graph.build_agent`` to return a fake
agent that yields ``AIMessageChunk`` objects.  This bypasses Bifrost and
deepagents entirely, exercising the run orchestrator, event bus, and SSE
layer without any external HTTP calls.

We patch ``rehketo.agent.run.build_agent``.  ``run.py`` uses
``from rehketo.agent.graph import build_agent``, which binds the name in
``rehketo.agent.run``'s module namespace.  That is the binding that
``run_agent`` calls; patching ``rehketo.agent.graph.build_agent`` would
not intercept it.

Assertions:
- At least one ``run.status`` event is emitted (transition to "running").
- Exactly one terminal ``run.status`` event with ``status="succeeded"``.
- At least one ``message.delta`` event carrying the streamed text.
- Exactly one ``message.complete`` event.
- An assistant ``Message`` row is persisted in the DB with the streamed content.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, Message, User, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class _HelloAgent:
    """Fake agent that streams 'hello' as two delta chunks."""

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        # stream_mode='messages' yields (AIMessageChunk, metadata) tuples.
        chunks = [
            AIMessageChunk(content="hel", id="msg-fake-1"),
            AIMessageChunk(content="lo", id="msg-fake-1"),
        ]
        for chunk in chunks:
            await asyncio.sleep(0)  # yield control to the event loop
            yield (chunk, {"langgraph_node": "agent"})


async def _fake_build_agent(run_id: str) -> AsyncIterator[_HelloAgent]:
    yield _HelloAgent()


@pytest.mark.asyncio
async def test_run_produces_streamed_assistant_message(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    # Patch build_agent in run.py's namespace — that is where the name is
    # bound after `from rehketo.agent.graph import build_agent`.
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

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "say hello"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        events: list[dict[str, Any]] = []
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]

    # Must see at least two run.status events (running + terminal).
    assert types.count("run.status") >= 2
    terminal = next(e for e in reversed(events) if e["type"] == "run.status")
    assert terminal["status"] == "succeeded"

    # Must see message deltas.
    assert any(e["type"] == "message.delta" for e in events)

    # Must see exactly one message.complete carrying the full MessageOut shape.
    complete_events = [e for e in events if e["type"] == "message.complete"]
    assert len(complete_events) == 1
    payload = complete_events[0]["message"]
    assert payload["role"] == "assistant"
    assert payload["conversation_id"] == str(conv.id)
    assert payload["run_id"] == run_id
    assert payload["id"]  # server-assigned UUID
    assert payload["created_at"]  # server-assigned timestamp
    assert "hello" in payload["content"]["text"].lower()

    # Last event is the run.ended terminator — that is what closes the stream.
    assert types[-1] == "run.ended"

    # Assistant message must be persisted with the full streamed content.
    fresh_engine = create_async_engine(db_url, future=True)
    maker = async_sessionmaker(fresh_engine, expire_on_commit=False)
    async with maker() as s:
        assistant = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == conv.id,
                    Message.role == "assistant",
                )
            )
        ).scalar_one_or_none()
    await fresh_engine.dispose()

    assert assistant is not None
    assert "hello" in (assistant.content or {}).get("text", "").lower()
