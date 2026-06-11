from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

from fastmcp import Client, FastMCP

from rehketo.db.models import McpServer
from rehketo.mcp import registry


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append((run_id, event))


def _echo_server() -> FastMCP:
    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    return server


def _row(name: str, url: str = "https://unused.example.com/mcp") -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=url,
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
    )


async def test_builds_tools_from_reachable_server(settings_env, monkeypatch) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=bus
        )
        assert [t.name for t in tools] == ["testsrv__echo"]
        result = await tools[0].ainvoke({"text": "hi"})

    assert result == "echo: hi"
    assert [e["type"] for _, e in bus.events] == ["tool.call", "tool.result"]


async def test_unreachable_server_is_skipped(settings_env, monkeypatch) -> None:
    good = _echo_server()

    def _client_for(server: McpServer) -> Client:  # type: ignore[type-arg]
        if server.name == "bad":
            raise RuntimeError("refused")
        return Client(good)

    monkeypatch.setattr(registry, "_client_for", _client_for)

    async with AsyncExitStack() as stack:
        tools = await registry.build_run_toolset(
            stack, [_row("bad"), _row("testsrv")], run_id="r1", bus=FakeBus()
        )
        assert [t.name for t in tools] == ["testsrv__echo"]


async def test_no_servers_yields_no_tools(settings_env) -> None:
    async with AsyncExitStack() as stack:
        assert (
            await registry.build_run_toolset(stack, [], run_id="r1", bus=FakeBus())
            == []
        )


async def test_invalid_combined_name_is_skipped(settings_env, monkeypatch) -> None:
    # fastmcp allows dots in tool names; the combined name "testsrv__bad.name"
    # breaks the provider regex.  Verify the tool is skipped, the valid tool
    # from the same server is kept, and the server does NOT abort the whole run.
    server = FastMCP("mixed")

    @server.tool(name="bad.name")
    def bad_tool(x: str) -> str:
        """A tool whose name contains a dot."""
        return x

    @server.tool
    def good_tool(x: str) -> str:
        """A tool with a valid name."""
        return x

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=bus
        )

    tool_names = [t.name for t in tools]
    # bad.name makes the combined name "testsrv__bad.name" — rejected by regex
    assert "testsrv__bad.name" not in tool_names
    # good_tool passes validation — retained
    assert "testsrv__good_tool" in tool_names
