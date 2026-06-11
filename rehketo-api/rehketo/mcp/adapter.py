"""Convert one MCP tool into a LangChain StructuredTool.

The wrapper coroutine is where tool.call / tool.result events are born —
published straight to the durable bus, never parsed out of LangGraph's
message stream, so the SSE schema stays decoupled from LangGraph internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.tools import StructuredTool
from mcp.types import TextContent

from rehketo.core.logging import format_exc_for_log, get_logger

if TYPE_CHECKING:
    from fastmcp import Client
    from fastmcp.client.client import CallToolResult
    from mcp.types import Tool

    from rehketo.runs.event_bus import RunEventBus

logger = get_logger(__name__)

# Cap the result payload in the *event* (bus + UI protection). The full
# text still returns to the model.
RESULT_EVENT_MAX_CHARS = 16_384
_TRUNCATION_MARKER = "\n…[truncated for event stream]"


def _result_text(result: CallToolResult) -> str:
    parts = [block.text for block in result.content if isinstance(block, TextContent)]
    return "\n".join(parts)


def _truncate_for_event(text: str) -> str:
    if len(text) <= RESULT_EVENT_MAX_CHARS:
        return text
    return text[:RESULT_EVENT_MAX_CHARS] + _TRUNCATION_MARKER


def build_structured_tool(
    *,
    server_name: str,
    tool: Tool,
    client: Client,  # type: ignore[type-arg]  # fastmcp.Client is Generic[T]; the transport type is opaque here
    run_id: str,
    bus: RunEventBus,
) -> StructuredTool:
    full_name = f"{server_name}__{tool.name}"

    async def _invoke(**kwargs: Any) -> str:
        call_id = str(uuid4())
        await bus.publish(
            run_id,
            {
                "type": "tool.call",
                "call_id": call_id,
                "tool": full_name,
                "arguments": kwargs,
            },
        )
        try:
            result = await client.call_tool(tool.name, kwargs, raise_on_error=False)
            text = _result_text(result)
            is_error = result.is_error
        except Exception as exc:
            # Transport/protocol failure mid-call. The error text goes back
            # to the model as the tool result so the agent can recover.
            logger.warning(
                "tool call failed server=%s tool=%s: %s",
                server_name,
                tool.name,
                format_exc_for_log(exc),
            )
            text = f"tool call failed: {exc}"
            is_error = True
        await bus.publish(
            run_id,
            {
                "type": "tool.result",
                "call_id": call_id,
                "result": _truncate_for_event(text),
                "is_error": is_error,
            },
        )
        return text

    return StructuredTool.from_function(
        coroutine=_invoke,
        name=full_name,
        description=tool.description or "",
        # langchain-core ≥1.4 accepts a JSON-schema dict directly — the MCP
        # inputSchema passes through with no pydantic model generation.
        args_schema=tool.inputSchema,
    )
