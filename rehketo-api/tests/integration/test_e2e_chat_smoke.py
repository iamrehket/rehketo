"""Full E2E smoke test — login → create conversation → post message → SSE → detail.

Uses a fake build_agent (same pattern as test_run_agent_end_to_end.py) so no
Bifrost / LLM network calls are made. Title generation is also patched to a
no-op so the test doesn't wait on a DNS failure to the mock Bifrost host.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


class _HiAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        yield (AIMessageChunk(content="hi", id="msg-e2e"), {"langgraph_node": "agent"})


async def _fake_build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[Any] = (),
    interrupt_on: Any = None,
    subagents: Any = None,
    skill_sources: Any = None,
) -> Any:
    yield _HiAgent()


async def _no_title(_cid: Any) -> None:
    return None


async def test_full_chat_turn(
    settings_env: object, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            "/auth/devonly/login",
            json={"email": "al@example.com", "roles": ["User"]},
        )
        assert r.status_code == 200
        sid = next(x for x in r.cookies.jar if x.name == SESSION_COOKIE).value
        csrf = next(x for x in r.cookies.jar if x.name == CSRF_COOKIE).value
        auth_cookies = {SESSION_COOKIE: sid, CSRF_COOKIE: csrf}
        auth_headers = {CSRF_HEADER: csrf}

        r = await c.post(
            "/conversations",
            json={},
            cookies=auth_cookies,
            headers=auth_headers,
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]

        r = await c.post(
            f"/conversations/{conv_id}/messages",
            json={"content": "hi there"},
            cookies=auth_cookies,
            headers=auth_headers,
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        # Consume SSE until terminal event
        events: list[dict[str, Any]] = []
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: sid},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        assert any(
            e["type"] == "run.status" and e.get("status") == "succeeded" for e in events
        )

        # Detail shows both user and assistant messages
        r = await c.get(
            f"/conversations/{conv_id}",
            cookies={SESSION_COOKIE: sid},
        )
        assert r.status_code == 200
        msgs = [i for i in r.json()["items"] if i["kind"] == "message"]
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles
