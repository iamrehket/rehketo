from __future__ import annotations

import contextlib
from typing import Any
from uuid import uuid4

from fastmcp import Client, FastMCP

from rehketo.db.models import McpServer, Skill
from rehketo.mcp import registry
from rehketo.mcp.skills import build_skill_subagents
from rehketo.runs.event_bus import PostgresEventBus


async def test_one_subagent_per_mcp_skill(settings_env, monkeypatch) -> None:
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List PRs."""
        return "[]"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=True,
    )
    skill = Skill(
        id=uuid4(),
        name="github",
        trigger="use when working with GitHub",
        kind="mcp",
        mcp_server_id=srv.id,
        allowed_roles=["User"],
        enabled=True,
    )

    bus = PostgresEventBus()
    async with contextlib.AsyncExitStack() as stack:
        subs = await build_skill_subagents(
            stack, [skill], [srv], run_id=str(uuid4()), bus=bus
        )

    assert len(subs) == 1
    sub: dict[str, Any] = subs[0]
    assert sub["name"] == "github"
    assert sub["description"] == "use when working with GitHub"
    assert [t.name for t in sub["tools"]] == ["github__list_prs"]
