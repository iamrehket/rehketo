"""Integration test — conversation.updated event fires when title generation
produces a new title. The protocol order is:
  message.complete
  run.status=succeeded   ← UI clears 'running' indicator here
  conversation.updated   ← fires after title gen awaits
  run.ended              ← SSE stream closes here

This frees the UI from waiting on title generation to stop showing a stale
'running' state after the reply is visible.

Patches both ``build_agent`` and ``generate_title_if_needed`` to avoid any
LLM calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class _HiAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        await asyncio.sleep(0)
        yield (AIMessageChunk(content="hi", id="m1"), {"langgraph_node": "agent"})


async def _fake_build_agent(run_id: str, system_prompt: str) -> AsyncIterator[_HiAgent]:
    yield _HiAgent()


async def _fake_title(_cid: Any) -> str:
    return "Mocked Title"


@pytest.mark.asyncio
async def test_conversation_updated_arrives_after_succeeded_before_ended(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _fake_title)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title=None)
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
            json={"content": "ping"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        events: list[dict[str, Any]] = []
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]

    assert "conversation.updated" in types
    updated = next(e for e in events if e["type"] == "conversation.updated")
    assert updated["conversation_id"] == str(conv.id)
    assert updated["title"] == "Mocked Title"

    # Ordering: message.complete → succeeded → conversation.updated → run.ended.
    complete_idx = types.index("message.complete")
    succeeded_idx = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "run.status" and e.get("status") == "succeeded"
    )
    updated_idx = types.index("conversation.updated")
    ended_idx = types.index("run.ended")
    assert complete_idx < succeeded_idx < updated_idx < ended_idx


@pytest.mark.asyncio
async def test_conversation_updated_not_emitted_when_title_unchanged(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When generate_title_if_needed returns None (title already set / bail),
    no conversation.updated event should be published."""
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    async def _no_title(_cid: Any) -> None:
        return None

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="Existing")
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
            json={"content": "ping"},
        )
        run_id = r.json()["run_id"]

        events: list[dict[str, Any]] = []
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert "conversation.updated" not in types
