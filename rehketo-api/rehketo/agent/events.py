from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage

if TYPE_CHECKING:
    from collections.abc import Iterator


def _stringify_content(content: object) -> str:
    """LangChain AIMessageChunk.content is typed `str | list[str | dict]`.
    Providers that emit content blocks (Anthropic native, some Responses-API
    shims) yield lists of {type: 'text', text: '...'} dicts. Flatten to plain
    text so downstream (SSE wire, message persistence) sees a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def transform_chunk(chunk: tuple[Any, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Convert a LangGraph `stream_mode='messages'` chunk into zero or more
    events in our stable schema. Yields nothing for empty / metadata-only
    chunks — and for non-AI messages: `stream_mode='messages'` also yields
    ToolMessages (raw tool output), which the MCP adapter already publishes
    as tool.call/tool.result events."""
    msg, _metadata = chunk
    if not isinstance(msg, AIMessage):
        return
    raw = msg.content
    delta = _stringify_content(raw)
    if not delta:
        return
    yield {
        "type": "message.delta",
        "message_id": msg.id,
        "delta": delta,
    }
