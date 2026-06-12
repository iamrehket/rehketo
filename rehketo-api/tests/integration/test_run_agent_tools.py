from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

import mcp.types
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
            auto_approve=True,
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
            # First delta arrives before the tool call.
            yield (AIMessageChunk(content="thinking…", id="msg-fake-1"), {})
            # Simulate the model deciding to call the tool mid-stream.
            await self._tools[0].ainvoke({"text": "hi"})
            # Second delta arrives after the tool result.
            yield (AIMessageChunk(content="done", id="msg-fake-1"), {})

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
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
    # Sequence order: first delta < tool.call < tool.result < last delta < run.ended
    first_delta = types.index("message.delta")
    tool_call_idx = types.index("tool.call")
    tool_result_idx = types.index("tool.result")
    last_delta = len(types) - 1 - types[::-1].index("message.delta")
    assert first_delta < tool_call_idx < tool_result_idx < last_delta
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
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[_QuietAgent]:
        captured["tools"] = list(tools)
        yield _QuietAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    # Server allows only Admin; the seeded user has role User.
    run_id = await _seed(db, server_roles=["Admin"])
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)
    assert captured["tools"] == []


async def test_dying_client_close_does_not_flip_run_to_failed(
    settings_env, db_url, db, monkeypatch
) -> None:
    """A client whose close() raises after the run succeeds must NOT flip the
    run row to 'failed'.  Regression for the enter_async_context path, which
    called __aexit__ and re-raised the dead session's exception into the outer
    except-Exception branch of run_agent."""

    class _DyingCloseClient:
        """Succeeds at connect + list_tools; raises on close()."""

        def __init__(self) -> None:
            self._tool = mcp.types.Tool(
                name="echo",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            )

        async def __aenter__(self) -> _DyingCloseClient:
            return self

        async def list_tools(self) -> list[mcp.types.Tool]:
            return [self._tool]

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            return f"echo: {arguments.get('text', '')}"

        async def close(self) -> None:
            raise RuntimeError("transport died mid-run")

        async def __aexit__(self, *_: object) -> None:
            await self.close()

    monkeypatch.setattr(registry, "_client_for", lambda s: _DyingCloseClient())

    class _QuietAgent:
        def __init__(self, tools: Sequence[Any]) -> None:
            pass

        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            yield (AIMessageChunk(content="ok", id="msg-fake-3"), {})

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[_QuietAgent]:
        yield _QuietAgent(tools)

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id = await _seed(db)
    bus = PostgresEventBus()
    # Must not raise — the run completes, then the stack closes the dying client.
    await run_mod.run_agent(run_id, bus)

    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
    assert row.status == "succeeded"
