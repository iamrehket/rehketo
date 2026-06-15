from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Client, FastMCP
from langchain_core.messages import AIMessageChunk

import rehketo.agent.run as run_mod
from rehketo.db.models import Conversation, McpServer, Run, Skill, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def test_run_agent_passes_skills_to_build_agent(
    settings_env, db_url, db, monkeypatch
) -> None:
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List PRs."""
        return "[]"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="queued",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://x/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=True,
    )
    db.add(srv)
    await db.commit()
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="github",
                trigger="GitHub repos",
                kind="mcp",
                mcp_server_id=srv.id,
                allowed_roles=["User"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="policy",
                trigger="reimburse",
                kind="doc",
                instructions="# Policy",
                allowed_roles=["User"],
                enabled=True,
            ),
        ]
    )
    await db.commit()

    captured: dict[str, Any] = {}

    class _QuietAgent:
        async def astream(
            self, stream_input: Any, **kwargs: Any
        ) -> AsyncGenerator[Any]:
            captured["stream_input"] = stream_input
            yield (AIMessageChunk(content="ok", id="m1"), {})

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
        subagents: Any = None,
        skill_sources: Any = None,
    ) -> AsyncIterator[_QuietAgent]:
        captured["main_tools"] = list(tools)
        captured["subagents"] = subagents
        captured["skill_sources"] = skill_sources
        yield _QuietAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    await run_mod.run_agent(run.id, PostgresEventBus())

    assert [s["name"] for s in captured["subagents"]] == ["github"]
    assert captured["skill_sources"] == ["/skills/"]
    assert "/skills/policy/SKILL.md" in captured["stream_input"]["files"]
    # The skill-backed server's tools live ONLY on the subagent, never on the
    # main agent — that is the progressive-disclosure guarantee.
    assert captured["main_tools"] == []
    assert [t.name for t in captured["subagents"][0]["tools"]] == ["github__list_prs"]
