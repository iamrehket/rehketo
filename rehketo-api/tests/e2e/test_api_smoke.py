"""Smoke test for the offline e2e infrastructure.

No Playwright yet — just exercises api_server + fake_bifrost + ui_build
over real HTTP. Catches fixture-level mistakes (wrong endpoint shape,
SSE parsing, port allocation, env propagation) before we layer the
browser tests on top. Auto-marked `@pytest.mark.e2e` by the conftest.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import pytest

if TYPE_CHECKING:
    from tests.e2e.fixtures.api_server import ApiHandle
    from tests.e2e.fixtures.bifrost_server import BifrostHandle


@pytest.mark.asyncio
async def test_full_chat_flow_through_real_http(
    api_server: ApiHandle, fake_bifrost: BifrostHandle
) -> None:
    """End-to-end via the real api on a real port talking to the fake bifrost.

    Validates: devonly login → cookies/CSRF flow → POST messages →
    SSE stream consumed → assistant message persisted → conversation
    detail returns ordered messages with the expected assembled text.
    """
    # Default profile = three chunks "Hello ", "world", "!"
    base = api_server.base_url
    async with httpx.AsyncClient(base_url=base, timeout=15.0) as c:
        # Reset fake bifrost to default profile (cheap idempotent).
        r = await c.post(
            f"{fake_bifrost.base_url}/__test__/mode".replace("/v1", ""),
            json={"profile": "default"},
        )
        # The fake server listens on a different base_url than the api;
        # build the absolute URL above. Status 200 expected.
        assert r.status_code == 200, r.text

        # 1. devonly login — sets session + CSRF cookies on the client jar.
        login = await c.post(
            "/auth/devonly/login",
            json={"email": "smoke@example.com", "roles": ["User"]},
        )
        assert login.status_code == 200, login.text
        assert "rehketo_session" in c.cookies
        assert "rehketo_csrf" in c.cookies
        csrf = c.cookies["rehketo_csrf"]

        # 2. Create a conversation.
        create = await c.post(
            "/conversations",
            headers={"X-CSRF-Token": csrf},
            json={},
        )
        assert create.status_code == 201, create.text
        conv_id = UUID(create.json()["id"])

        # 3. Kick off a run.
        post_msg = await c.post(
            f"/conversations/{conv_id}/messages",
            headers={"X-CSRF-Token": csrf},
            json={"content": "hi"},
        )
        assert post_msg.status_code == 202, post_msg.text
        run_id = post_msg.json()["run_id"]

        # 4. Stream SSE until run.ended.
        events: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(15):
                async with c.stream("GET", f"/runs/{run_id}/events") as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        events.append(json.loads(line[6:]))
                        if events[-1].get("type") == "run.ended":
                            break
        except TimeoutError:
            pytest.fail(
                f"SSE stream did not terminate; saw {[e['type'] for e in events]}"
            )

        types = [e["type"] for e in events]
        assert types[-1] == "run.ended", f"types={types}"
        assert types.count("run.ended") == 1

        # 5. The assembled assistant text matches the fake's chunks.
        msg_completes = [e for e in events if e["type"] == "message.complete"]
        assert len(msg_completes) == 1
        assistant_text = msg_completes[0]["message"]["content"]["text"]
        assert "Hello world!" in assistant_text, f"got {assistant_text!r}"

        # 6. Conversation detail returns transcript items in time order (user
        # first, then assistant) — same property the Phase A regression test
        # pins via ASGITransport, now verified over a real socket. M3 replaced
        # the flat `messages` field with the `items` discriminated union.
        detail = await c.get(f"/conversations/{conv_id}")
        assert detail.status_code == 200, detail.text
        items = detail.json()["items"]
        roles = [i["role"] for i in items if i["kind"] == "message"]
        assert roles == ["user", "assistant"], f"roles={roles}"
