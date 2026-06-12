from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

import mcp.types
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


def _row(
    name: str,
    url: str = "https://unused.example.com/mcp",
    *,
    auto_approve: bool = False,
) -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=url,
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=auto_approve,
    )


async def test_builds_tools_from_reachable_server(settings_env, monkeypatch) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools, _interrupt_on = await registry.build_run_toolset(
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
        tools, _interrupt_on = await registry.build_run_toolset(
            stack, [_row("bad"), _row("testsrv")], run_id="r1", bus=FakeBus()
        )
        assert [t.name for t in tools] == ["testsrv__echo"]


async def test_no_servers_yields_no_tools(settings_env) -> None:
    async with AsyncExitStack() as stack:
        tools, interrupt_on = await registry.build_run_toolset(
            stack, [], run_id="r1", bus=FakeBus()
        )
    assert tools == []
    assert interrupt_on == {}


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
        tools, _interrupt_on = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=bus
        )

    tool_names = [t.name for t in tools]
    # bad.name makes the combined name "testsrv__bad.name" — rejected by regex
    assert "testsrv__bad.name" not in tool_names
    # good_tool passes validation — retained
    assert "testsrv__good_tool" in tool_names


# ---------------------------------------------------------------------------
# Item 1: fullmatch — trailing newline in tool name must be skipped
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal stub client that returns a hand-constructed mcp.types.Tool list.

    FastMCP refuses to register tools with control characters in their names,
    so we bypass that by injecting the mcp.types.Tool directly via list_tools.
    """

    def __init__(self, tools: list[mcp.types.Tool]) -> None:
        self._tools = tools

    async def list_tools(self) -> list[mcp.types.Tool]:
        return self._tools

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


async def test_tool_name_with_trailing_newline_is_skipped(
    settings_env, monkeypatch
) -> None:
    """A tool whose name ends with \\n must be skipped — fullmatch rejects it."""
    bad_tool = mcp.types.Tool(
        name="bad\n",
        inputSchema={"type": "object", "properties": {}},
    )
    good_tool = mcp.types.Tool(
        name="good",
        inputSchema={"type": "object", "properties": {}},
    )
    stub = _StubClient([bad_tool, good_tool])
    monkeypatch.setattr(registry, "_client_for", lambda s: stub)

    async with AsyncExitStack() as stack:
        tools, _interrupt_on = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=FakeBus()
        )

    tool_names = [t.name for t in tools]
    assert "testsrv__bad\n" not in tool_names
    assert "testsrv__good" in tool_names


# ---------------------------------------------------------------------------
# Item 2: connect timeout — hung server is skipped, others survive
# ---------------------------------------------------------------------------


class _HungClient:
    """Client whose __aenter__ never returns (simulates a hung TCP connect)."""

    async def list_tools(
        self,
    ) -> list[mcp.types.Tool]:  # pragma: no cover - unreachable
        raise AssertionError("should not reach list_tools")

    async def __aenter__(self) -> _HungClient:
        await asyncio.sleep(9999)
        return self  # pragma: no cover

    async def __aexit__(self, *_: object) -> None:
        pass


async def test_hung_server_is_skipped_others_survive(settings_env, monkeypatch) -> None:
    """A server that hangs on connect must be skipped after the timeout; the
    next server in the list must still contribute its tools."""
    good = _echo_server()

    def _client_for(server: McpServer) -> object:
        if server.name == "hung":
            return _HungClient()
        return Client(good)

    monkeypatch.setattr(registry, "_client_for", _client_for)
    monkeypatch.setattr(registry, "_SERVER_CONNECT_TIMEOUT_S", 0.05)

    async with AsyncExitStack() as stack:
        tools, _interrupt_on = await registry.build_run_toolset(
            stack, [_row("hung"), _row("testsrv")], run_id="r1", bus=FakeBus()
        )

    assert [t.name for t in tools] == ["testsrv__echo"]


# ---------------------------------------------------------------------------
# Item 4: corrupt ciphertext — decrypt_token raises, server is skipped
# ---------------------------------------------------------------------------


async def test_corrupt_ciphertext_server_is_skipped(settings_env) -> None:
    """McpServer row with garbage ciphertext → decrypt_token raises inside the
    per-server try → server skipped, result is empty list, no exception escapes."""
    bad_row = McpServer(
        id=uuid4(),
        name="broken",
        url="https://unused.example.com/mcp",
        auth_token_ct=b"garbage",
        allowed_roles=["User"],
        enabled=True,
        auto_approve=False,
    )

    async with AsyncExitStack() as stack:
        tools, interrupt_on = await registry.build_run_toolset(
            stack, [bad_row], run_id="r1", bus=FakeBus()
        )

    assert tools == []
    assert interrupt_on == {}


# ---------------------------------------------------------------------------
# Item 3: dying-close stub — stack exit must NOT propagate the close error
# ---------------------------------------------------------------------------


class _DyingCloseClient:
    """Stub whose __aenter__ + list_tools succeed but close() raises.

    Models a tool server that dies mid-run: the transport has gone away by
    the time we try to close it.
    """

    def __init__(self, tools: list[mcp.types.Tool]) -> None:
        self._tools = tools

    async def __aenter__(self) -> _DyingCloseClient:
        return self

    async def list_tools(self) -> list[mcp.types.Tool]:
        return self._tools

    async def close(self) -> None:
        raise RuntimeError("transport died mid-run")

    async def __aexit__(self, *_: object) -> None:
        # Re-raises to prove the test would fail against the old code path.
        await self.close()


async def test_dying_close_does_not_escape_stack(settings_env, monkeypatch) -> None:
    """Stack exit must not propagate a close() error from a mid-run server death.

    The old code used stack.enter_async_context(client), which calls __aexit__
    on unwind and re-raises the dead session's exception, flipping a succeeded
    run to failed.  The fix uses client.__aenter__() + push_async_callback to
    _close_client, which swallows the error.  This test fails (RuntimeError
    escapes) against the old path and passes against the new one.
    """
    good_tool = mcp.types.Tool(
        name="good",
        inputSchema={"type": "object", "properties": {}},
    )
    stub = _DyingCloseClient([good_tool])
    monkeypatch.setattr(registry, "_client_for", lambda s: stub)

    # Must not raise — the tool list is built, then the stack exits cleanly
    # despite close() raising RuntimeError.
    async with AsyncExitStack() as stack:
        tools, _interrupt_on = await registry.build_run_toolset(
            stack, [_row("testsrv")], run_id="r1", bus=FakeBus()
        )
        assert [t.name for t in tools] == ["testsrv__good"]


async def test_interrupt_on_built_from_auto_approve(settings_env, monkeypatch) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    bus = FakeBus()

    async with AsyncExitStack() as stack:
        tools, interrupt_on = await registry.build_run_toolset(
            stack,
            [_row("untrusted"), _row("trusted", auto_approve=True)],
            run_id="r1",
            bus=bus,
        )
    assert {t.name for t in tools} == {"untrusted__echo", "trusted__echo"}
    # Only tools from auto_approve=False servers require review; approve and
    # reject are the M3.5 decision vocabulary (no edit/respond).
    assert set(interrupt_on) == {"untrusted__echo"}
    assert interrupt_on["untrusted__echo"]["allowed_decisions"] == ["approve", "reject"]


async def test_all_trusted_servers_yield_empty_interrupt_on(
    settings_env, monkeypatch
) -> None:
    server = _echo_server()
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    async with AsyncExitStack() as stack:
        _tools, interrupt_on = await registry.build_run_toolset(
            stack, [_row("trusted", auto_approve=True)], run_id="r1", bus=FakeBus()
        )
    assert interrupt_on == {}
