"""Real-graph HITL canary: create_deep_agent + HumanInTheLoopMiddleware +
AsyncPostgresSaver, driven by a scripted tool-calling fake model. Pins the
interrupt payload shape and the Command(resume=...) format M3.5 relies on.
No LLM cost. Run explicitly:

    uv run pytest -m live_deps tests/integration/test_run_agent_approval_live.py
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pydantic
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import text

import rehketo.agent.graph as graph_mod
import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

pytestmark = pytest.mark.live_deps


class _ScriptedModel(BaseChatModel):
    """Scripted BaseChatModel — returns pre-defined AIMessages in sequence.

    Used instead of GenericFakeChatModel because that class streams via
    _stream() which splits content on whitespace and does not handle the
    modern tool_calls field; deepagents calls ainvoke() which dispatches
    to _generate(), which this class implements directly. Raises StopIteration
    on exhaustion; the executor wrapper converts it, surfacing as RuntimeError
    — extend the responses list if deepagents makes extra model calls.
    """

    responses: list[AIMessage]
    _call_idx: int = pydantic.PrivateAttr(default=0)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = self._call_idx
        if idx >= len(self.responses):
            msg = (
                f"_ScriptedModel ran out of responses "
                f"(call {idx}, have {len(self.responses)}). "
                "Extend the responses list if deepagents makes extra model calls."
            )
            raise StopIteration(msg)
        self._call_idx += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])

    @property
    def _llm_type(self) -> str:
        return "scripted-model"


async def _seed(db: Any) -> Any:
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
            auto_approve=False,
        )
    )
    await db.commit()
    return run.id


async def _wait_for_status(run_id: Any, status: str, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            async with sessionmaker()() as s:
                row = (
                    await s.execute(
                        text("SELECT status FROM runs WHERE id = :rid"),
                        {"rid": str(run_id)},
                    )
                ).one()
            if row.status == status:
                return
            await asyncio.sleep(0.1)


async def test_real_graph_pauses_and_resumes_on_approval(
    settings_env: Any, db_url: str, db: Any, monkeypatch: Any
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    model = _ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "testsrv__echo",
                        "args": {"text": "hi"},
                        "id": "c1",
                    }
                ],
            ),
            AIMessage(content="the tool said hi back"),
        ]
    )
    monkeypatch.setattr(graph_mod, "build_chat_model", lambda: model)

    run_id = await _seed(db)
    # No start(): subscribe() falls back to 0.1s polling — no LISTEN task needed here.
    bus = PostgresEventBus(poll_interval=0.1)
    task = asyncio.create_task(run_mod.run_agent(run_id, bus))

    await _wait_for_status(run_id, "pending_approval")
    async with sessionmaker()() as s:
        payload = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.approval_required'"
                ),
                {"rid": str(run_id)},
            )
        ).one()
    assert payload.payload["tool"] == "testsrv__echo"
    assert payload.payload["arguments"] == {"text": "hi"}
    await bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": payload.payload["approval_id"],
            "decision": "approve",
        },
    )
    await asyncio.wait_for(task, timeout=60)

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
        status_row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"),
                {"rid": str(run_id)},
            )
        ).one()
    types = [r.payload["type"] for r in rows]
    # The approved tool actually executed: adapter events flowed as in M3.
    assert "tool.call" in types
    assert "tool.result" in types
    assert status_row.status == "succeeded"
