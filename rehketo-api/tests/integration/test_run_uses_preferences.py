"""Run orchestration must read the user's stored custom instructions at run
start, assemble them into the system prompt, and pass that to build_agent.

Patches ``rehketo.agent.run.build_agent`` (the binding run_agent actually
calls — see test_run_agent_end_to_end.py for why) with a fake that captures
the prompt it was given.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from rehketo.agent.prompt import BASE_SYSTEM_PROMPT
from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserPreferences, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

captured: dict[str, str] = {}


class _OkAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        await asyncio.sleep(0)
        yield (AIMessageChunk(content="ok", id="m1"), {"langgraph_node": "agent"})


async def _fake_build_agent(
    run_id: str, system_prompt: str, tools: Sequence[Any] = ()
) -> AsyncIterator[_OkAgent]:
    captured["system_prompt"] = system_prompt
    yield _OkAgent()


async def _post_and_drain(c: AsyncClient, conv_id: str, sid: str, csrf: str) -> None:
    r = await c.post(
        f"/conversations/{conv_id}/messages",
        cookies={SESSION_COOKIE: sid, CSRF_COOKIE: csrf},
        headers={CSRF_HEADER: csrf},
        json={"content": "hi"},
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    async with c.stream(
        "GET", f"/runs/{run_id}/events", cookies={SESSION_COOKIE: sid}
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if (
                line.startswith("data: ")
                and json.loads(line[6:])["type"] == "run.ended"
            ):
                break


async def _seed(db: AsyncSession, *, instructions: str | None) -> tuple[str, str, str]:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)
    await db.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    rows: list[object] = [UserRole(user_id=u.id, role="User"), conv]
    if instructions is not None:
        rows.append(UserPreferences(user_id=u.id, custom_instructions=instructions))
    db.add_all(rows)
    await db.commit()
    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    return str(conv.id), str(sid), issue_csrf_token(str(sid))


async def test_run_passes_assembled_prompt(
    settings_env: object,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()
    captured.clear()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    conv_id, sid, csrf = await _seed(db, instructions="Answer in haiku.")

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _post_and_drain(c, conv_id, sid, csrf)

    prompt = captured["system_prompt"]
    assert prompt.startswith(BASE_SYSTEM_PROMPT)
    assert "## User instructions" in prompt
    assert "Answer in haiku." in prompt


async def test_run_without_preferences_uses_base_prompt(
    settings_env: object,
    db_url: str,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()
    captured.clear()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    conv_id, sid, csrf = await _seed(db, instructions=None)

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        await _post_and_drain(c, conv_id, sid, csrf)

    assert captured["system_prompt"] == BASE_SYSTEM_PROMPT
