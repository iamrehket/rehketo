from __future__ import annotations

from typing import Any

from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp.types import TextContent, Tool

from rehketo.mcp.adapter import (
    RESULT_EVENT_MAX_CHARS,
    build_structured_tool,
)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append((run_id, event))


class FakeClient:
    def __init__(
        self,
        *,
        text: str | None = "ok",
        is_error: bool = False,
        raise_exc: Exception | None = None,
        structured: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._text = text
        self._is_error = is_error
        self._raise = raise_exc
        self._structured = structured

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, raise_on_error: bool = True
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        if self._is_error and raise_on_error:
            raise ToolError(self._text or "error")
        content = (
            [TextContent(type="text", text=self._text)]
            if self._text is not None
            else []
        )
        return CallToolResult(
            content=content,
            structured_content=self._structured,
            meta=None,
            is_error=self._is_error,
        )


def _tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text back.",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


async def test_success_publishes_call_then_result() -> None:
    bus, client = FakeBus(), FakeClient(text="echo: hi")
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    assert tool.name == "testsrv__echo"
    assert tool.description == "Echo text back."
    assert tool.args_schema == _tool().inputSchema

    result = await tool.ainvoke({"text": "hi"})

    assert result == "echo: hi"
    assert client.calls == [("echo", {"text": "hi"})]
    types = [e["type"] for _, e in bus.events]
    assert types == ["tool.call", "tool.result"]
    call_event, result_event = bus.events[0][1], bus.events[1][1]
    assert call_event["tool"] == "testsrv__echo"
    assert call_event["arguments"] == {"text": "hi"}
    assert result_event["call_id"] == call_event["call_id"]
    assert result_event["result"] == "echo: hi"
    assert result_event["is_error"] is False


async def test_mcp_error_result_sets_is_error() -> None:
    bus = FakeBus()
    client = FakeClient(text="boom", is_error=True)
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert result == "boom"
    assert bus.events[1][1]["is_error"] is True


async def test_transport_exception_becomes_error_result() -> None:
    bus = FakeBus()
    client = FakeClient(raise_exc=RuntimeError("connection lost"))
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert "connection lost" in result
    assert bus.events[1][1]["is_error"] is True


async def test_result_event_is_truncated_but_return_is_not() -> None:
    big = "x" * (RESULT_EVENT_MAX_CHARS + 1000)
    bus, client = FakeBus(), FakeClient(text=big)
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert result == big  # full text goes back to the model
    event_result = bus.events[1][1]["result"]
    assert len(event_result) < len(big)
    assert "truncated" in event_result


async def test_structured_content_fallback_when_no_text_parts() -> None:
    # Non-conforming servers may omit the text mirror of structured output;
    # the adapter must still return the JSON payload to the model.
    bus = FakeBus()
    client = FakeClient(text=None, structured={"answer": 42})
    tool = build_structured_tool(
        server_name="testsrv", tool=_tool(), client=client, run_id="r1", bus=bus
    )
    result = await tool.ainvoke({"text": "hi"})
    assert result == '{"answer": 42}'
