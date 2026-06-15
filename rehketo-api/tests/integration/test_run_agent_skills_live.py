"""Live skill-delegation canary: run_agent + real deepagents graph + real postgres
checkpointer, driven by a scripted model. Proves the full skill subagent path:

  main agent → task(subagent_type="github") → github subagent
  → github__list_prs tool fires → result streams as tool.call / tool.result events.

No LLM cost. Run explicitly:

    uv run pytest -m live_deps tests/integration/test_run_agent_skills_live.py
"""

from __future__ import annotations

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
from rehketo.db.models import Conversation, McpServer, Run, Skill, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

pytestmark = pytest.mark.live_deps

# ---------------------------------------------------------------------------
# Scripted model
# ---------------------------------------------------------------------------


class _ScriptedModel(BaseChatModel):
    """Returns pre-defined AIMessages in sequence.

    Shared between the main agent and its skill subagents (deepagents re-uses
    the same model instance for subagents that omit 'model'). Raises
    StopIteration on exhaustion so over-calling surfaces immediately.
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
            raise RuntimeError(
                "scripted model exhausted: more LLM calls than scripted turns"
            )
        self._call_idx += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])

    @property
    def _llm_type(self) -> str:
        return "scripted-model"


# ---------------------------------------------------------------------------
# Seed helper
# ---------------------------------------------------------------------------


async def _seed(db: Any) -> Any:
    """Seed user(role=User)/conversation/run + github McpServer + mcp-skill."""
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
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://unused.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=True,
    )
    db.add(srv)
    await db.commit()
    db.add(
        Skill(
            id=uuid4(),
            name="github",
            trigger="Use for GitHub pull-request queries",
            kind="mcp",
            mcp_server_id=srv.id,
            allowed_roles=["User"],
            enabled=True,
        )
    )
    await db.commit()
    return run.id


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_skill_delegation_fires_mcp_tool(
    settings_env: Any, db_url: str, db: Any, monkeypatch: Any
) -> None:
    """Scripted model causes the main agent to delegate to the github subagent,
    which calls github__list_prs and gets back a recognisable result string.
    The adapter must emit tool.call and tool.result events for the MCP call."""
    from fastmcp import Client, FastMCP

    # ---- in-memory MCP server with a recognisable list_prs tool ----------
    server = FastMCP("github")

    @server.tool
    def list_prs() -> str:
        """List open pull requests."""
        return "PR #1: fix bug"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    # ---- four scripted turns (main→task, sub→list_prs, sub→answer, main→answer) ----
    #
    # Turn 0 (main agent): emit task tool call to delegate to the github subagent.
    # Turn 1 (subagent):   emit github__list_prs tool call.
    # Turn 2 (subagent):   final answer after seeing the tool result.
    # Turn 3 (main agent): final answer after the task tool returns.
    #
    # deepagents reuses the parent's model instance for subagents that omit the
    # 'model' key; build_skill_subagents intentionally omits it so the same
    # _ScriptedModel advances through all four turns in sequence.
    model = _ScriptedModel(
        responses=[
            # Turn 0 — main agent delegates to the github subagent.
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {
                            "description": "List open pull requests from GitHub.",
                            "subagent_type": "github",
                        },
                        "id": "tc-main-1",
                    }
                ],
                id="msg-main-1",
            ),
            # Turn 1 — github subagent calls the real MCP tool.
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "github__list_prs",
                        "args": {},
                        "id": "tc-sub-1",
                    }
                ],
                id="msg-sub-1",
            ),
            # Turn 2 — github subagent summarises the result.
            AIMessage(content="Found PR #1: fix bug", id="msg-sub-2"),
            # Turn 3 — main agent reports back to the user.
            AIMessage(
                content="The GitHub subagent found: PR #1: fix bug", id="msg-main-2"
            ),
        ]
    )
    monkeypatch.setattr(graph_mod, "build_chat_model", lambda: model)

    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)
    await run_mod.run_agent(run_id, bus)

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
    assert "tool.call" in types, f"expected tool.call in events; got {types}"
    assert "tool.result" in types, f"expected tool.result in events; got {types}"

    result_event = next(r.payload for r in rows if r.payload["type"] == "tool.result")
    assert "PR #1" in result_event["result"], (
        f"expected 'PR #1' in tool.result payload; got {result_event['result']!r}"
    )
    assert result_event["is_error"] is False

    assert status_row.status == "succeeded", f"run ended in {status_row.status!r}"
