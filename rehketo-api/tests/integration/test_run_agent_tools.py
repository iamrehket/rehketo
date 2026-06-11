from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def _seed(db, *, server_roles: list[str] | None = None) -> Any:
    """Seed user(role=User)/conversation/run + one enabled MCP server row.

    server_roles defaults to ["User"] (the seeded user can use it).
    """
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
    db.add(
        McpServer(
            id=uuid4(),
            name="testsrv",
            url="https://unused.example.com/mcp",
            auth_token_ct=None,
            allowed_roles=server_roles if server_roles is not None else ["User"],
            enabled=True,
        )
    )
    await db.commit()
    return run.id


async def test_run_agent_executes_tools_and_streams_events(
    settings_env, db_url, db, monkeypatch
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    captured: dict[str, Any] = {}

    class _ToolCallingAgent:
        def __init__(self, tools: Sequence[Any]) -> None:
            self._tools = tools

        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            # Simulate the model deciding to call the tool mid-stream.
            await self._tools[0].ainvoke({"text": "hi"})
            yield (AIMessageChunk(content="done", id="msg-fake-1"), {})

    async def _fake_build_agent(
        run_id: str, system_prompt: str, tools: Sequence[Any] = ()
    ) -> AsyncIterator[_ToolCallingAgent]:
        captured["tools"] = list(tools)
        yield _ToolCallingAgent(tools)

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id = await _seed(db)
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)

    assert [t.name for t in captured["tools"]] == ["testsrv__echo"]

    async with sessionmaker()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    types = [r.payload["type"] for r in rows]
    assert "tool.call" in types
    assert "tool.result" in types
    assert types.index("tool.call") < types.index("tool.result")
    assert types[-1] == "run.ended"
    result_event = next(r.payload for r in rows if r.payload["type"] == "tool.result")
    assert result_event["result"] == "echo: hi"
    assert result_event["is_error"] is False


async def test_user_without_server_role_gets_no_tools(
    settings_env, db_url, db, monkeypatch
) -> None:
    captured: dict[str, Any] = {}

    class _QuietAgent:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            yield (AIMessageChunk(content="hello", id="msg-fake-2"), {})

    async def _fake_build_agent(
        run_id: str, system_prompt: str, tools: Sequence[Any] = ()
    ) -> AsyncIterator[_QuietAgent]:
        captured["tools"] = list(tools)
        yield _QuietAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    # Server allows only Admin; the seeded user has role User.
    run_id = await _seed(db, server_roles=["Admin"])
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)
    assert captured["tools"] == []
