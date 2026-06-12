from __future__ import annotations

from langchain_core.messages import AIMessageChunk, ToolMessage

from rehketo.agent.events import transform_chunk


def test_message_delta_chunk_emits_message_delta() -> None:
    chunk = AIMessageChunk(content="hello ", id="msg-1")
    events = list(transform_chunk((chunk, {"langgraph_node": "agent"})))
    assert len(events) == 1
    assert events[0]["type"] == "message.delta"
    assert events[0]["message_id"] == "msg-1"
    assert events[0]["delta"] == "hello "


def test_empty_chunk_emits_nothing() -> None:
    assert list(transform_chunk((AIMessageChunk(content="", id="msg-1"), {}))) == []


def test_tool_message_emits_nothing() -> None:
    """ToolMessages carry raw MCP output; the adapter already publishes them
    as tool.call/tool.result. Streaming them as deltas is the clobbering bug."""
    msg = ToolMessage(content="raw tool output", tool_call_id="call_0", id="t-1")
    assert list(transform_chunk((msg, {}))) == []


def test_list_of_text_blocks_is_concatenated() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ],
        id="msg-2",
    )
    events = list(transform_chunk((chunk, {})))
    assert len(events) == 1
    assert events[0]["delta"] == "hello world"
    assert events[0]["message_id"] == "msg-2"


def test_list_of_plain_strings_is_concatenated() -> None:
    chunk = AIMessageChunk(content=["foo", "bar"], id="msg-3")
    events = list(transform_chunk((chunk, {})))
    assert events[0]["delta"] == "foobar"


def test_list_with_non_text_blocks_is_skipped() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "tool_use", "id": "t1", "input": {}, "name": "x"},
            {"type": "text", "text": "after tool"},
        ],
        id="msg-4",
    )
    events = list(transform_chunk((chunk, {})))
    assert events[0]["delta"] == "after tool"


def test_empty_list_content_emits_nothing() -> None:
    assert list(transform_chunk((AIMessageChunk(content=[], id="msg-5"), {}))) == []
