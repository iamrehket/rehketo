from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, ToolMessage
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.agent.run import _load_history
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def _seed(db) -> Any:
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
            allowed_roles=["User"],
            enabled=True,
            auto_approve=True,
        )
    )
    await db.commit()
    return run.id, conv.id


async def test_two_turn_run_persists_thinking_and_answer_rows(
    settings_env, db_url, db, monkeypatch
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    class _TwoTurnAgent:
        def __init__(self, tools: Sequence[Any]) -> None:
            self._tools = tools

        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            # Turn 1: narration, then the tool call.
            yield (AIMessageChunk(content="let me check ", id="turn-1"), {})
            yield (AIMessageChunk(content="the weather", id="turn-1"), {})
            await self._tools[0].ainvoke({"text": "boise"})
            # LangGraph also yields the ToolMessage on this stream mode; the
            # transform must drop it (the clobbering bug).
            yield (ToolMessage(content="echo: boise", tool_call_id="c1"), {})
            # Turn 2: the answer.
            yield (AIMessageChunk(content="It is sunny.", id="turn-2"), {})

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
        subagents: Any = None,
        skill_sources: Any = None,
    ) -> AsyncIterator[_TwoTurnAgent]:
        yield _TwoTurnAgent(tools)

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id, conv_id = await _seed(db)
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)

    # --- Persistence: one thinking row + one answer row, correctly ordered.
    async with sessionmaker()() as s:
        msg_rows = (
            await s.execute(
                text(
                    "SELECT content, created_at FROM messages "
                    "WHERE run_id = :rid AND role = 'assistant' "
                    "ORDER BY created_at"
                ),
                {"rid": str(run_id)},
            )
        ).all()
        tool_call_at = (
            await s.execute(
                text(
                    "SELECT created_at FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.call'"
                ),
                {"rid": str(run_id)},
            )
        ).scalar_one()

    assert len(msg_rows) == 2
    thinking, answer = msg_rows
    assert thinking.content == {
        "text": "let me check the weather",
        "channel": "thinking",
    }
    assert answer.content == {"text": "It is sunny."}
    # Narration interleaves BEFORE the tool row it triggered. <= because
    # the last delta event insert and the tool.call insert share the DB clock;
    # microsecond equality is possible and renders correctly (stable sort).
    assert thinking.created_at <= tool_call_at

    # --- No leak: the tool output appears in no message row.
    assert all("echo:" not in str(r.content) for r in msg_rows)

    # --- Events: a message.complete per row, answer last; no delta carries
    # the tool output.
    async with sessionmaker()() as s:
        events = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    payloads = [r.payload for r in events]
    completes = [p for p in payloads if p["type"] == "message.complete"]
    assert len(completes) == 2
    assert completes[0]["message"]["content"]["channel"] == "thinking"
    assert "channel" not in completes[1]["message"]["content"]
    deltas = [p for p in payloads if p["type"] == "message.delta"]
    assert all("echo:" not in p["delta"] for p in deltas)

    # --- History: only the answer feeds back to the model.
    async with sessionmaker()() as s:
        history = await _load_history(s, conv_id)
    assert [m.content for m in history] == ["It is sunny."]


async def test_failed_run_persists_segments_under_same_rule(
    settings_env, db_url, db, monkeypatch
) -> None:
    """A mid-run failure persists completed turns as thinking and the
    partial tail as the answer — same rule as success."""

    class _FailingAgent:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            yield (AIMessageChunk(content="narration", id="turn-1"), {})
            yield (AIMessageChunk(content="partial answ", id="turn-2"), {})
            raise RuntimeError("provider exploded")

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
        subagents: Any = None,
        skill_sources: Any = None,
    ) -> AsyncIterator[_FailingAgent]:
        yield _FailingAgent()

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id, _conv_id = await _seed(db)
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)

    async with sessionmaker()() as s:
        status = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"),
                {"rid": str(run_id)},
            )
        ).scalar_one()
        msg_rows = (
            await s.execute(
                text(
                    "SELECT content FROM messages "
                    "WHERE run_id = :rid AND role = 'assistant' "
                    "ORDER BY created_at"
                ),
                {"rid": str(run_id)},
            )
        ).all()

    assert status == "failed"
    assert len(msg_rows) == 2
    assert msg_rows[0].content == {"text": "narration", "channel": "thinking"}
    assert msg_rows[1].content == {"text": "partial answ"}
