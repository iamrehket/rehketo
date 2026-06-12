"""run_agent's resume loop against a fake agent that emulates the deepagents
HITL contract: first astream stint ends with a pending interrupt; the resume
input must be a Command carrying {"decisions": [...]} keyed by interrupt id;
the second stint yields the final text. The REAL middleware contract is
pinned separately by the live_deps test in test_run_agent_approval_live.py
(next task)."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langgraph.types import Interrupt
from sqlalchemy import text

import rehketo.agent.run as run_mod
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
            auto_approve=False,  # the M3.5 path under test
        )
    )
    await db.commit()
    return run.id


class _InterruptingAgent:
    """First astream stint interrupts; resume stint records the Command."""

    def __init__(self) -> None:
        self.resume_inputs: list[Any] = []
        self._streamed_once = False
        self.interrupt = Interrupt(
            value={
                "action_requests": [{"name": "testsrv__echo", "args": {"text": "hi"}}],
                "review_configs": [
                    {
                        "action_name": "testsrv__echo",
                        "allowed_decisions": ["approve", "reject"],
                    }
                ],
            },
            id="intr-1",
        )

    async def astream(
        self, stream_input: Any, *, config: Any = None, stream_mode: Any = None
    ) -> AsyncGenerator[Any]:
        if not self._streamed_once:
            self._streamed_once = True
            yield (AIMessageChunk(content="calling…", id="m1"), {})
            return  # graph paused on the interrupt
        self.resume_inputs.append(stream_input)
        yield (AIMessageChunk(content="done", id="m1"), {})

    async def aget_state(self, config: Any) -> Any:
        if self._streamed_once and not self.resume_inputs:
            return SimpleNamespace(
                tasks=(SimpleNamespace(interrupts=(self.interrupt,)),)
            )
        return SimpleNamespace(tasks=())


def _install(monkeypatch, agent: _InterruptingAgent) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[_InterruptingAgent]:
        assert interrupt_on, "auto_approve=False server must produce interrupt config"
        yield agent

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)


async def _event_payloads(run_id) -> list[dict[str, Any]]:
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
    return [r.payload for r in rows]


async def _wait_for_status(run_id, status: str, timeout: float = 10.0) -> None:
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
            await asyncio.sleep(0.05)


async def _decide(bus, run_id, decision: str) -> str:
    """Find the published approval id, publish a decision for it."""
    payloads = await _event_payloads(run_id)
    required = next(p for p in payloads if p["type"] == "tool.approval_required")
    approval_id = required["approval_id"]
    await bus.publish(
        str(run_id),
        {
            "type": "tool.approval_decision",
            "approval_id": approval_id,
            "decision": decision,
        },
    )
    return approval_id


async def test_approve_resumes_and_succeeds(
    settings_env, db_url, db, monkeypatch
) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    await _decide(bus, run_id, "approve")
    await asyncio.wait_for(task, timeout=10)

    # Resume payload matches the middleware contract, keyed by interrupt id.
    assert len(agent.resume_inputs) == 1
    cmd = agent.resume_inputs[0]
    assert cmd.resume == {"intr-1": {"decisions": [{"type": "approve"}]}}

    payloads = await _event_payloads(run_id)
    types = [p["type"] for p in payloads]
    i_req = types.index("tool.approval_required")
    i_dec = types.index("tool.approval_decision")
    statuses = [p.get("status") for p in payloads if p["type"] == "run.status"]
    assert statuses == ["running", "pending_approval", "running", "succeeded"]
    assert i_req < i_dec
    assert types[-1] == "run.ended"
    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
        msg = (
            await s.execute(
                text("SELECT content FROM messages WHERE run_id = :rid"),
                {"rid": str(run_id)},
            )
        ).one()
    assert row.status == "succeeded"
    # Both stints' text assembled into the persisted assistant message.
    assert msg.content["text"] == "calling…done"


async def test_deny_maps_to_reject(settings_env, db_url, db, monkeypatch) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    await _decide(bus, run_id, "deny")
    await asyncio.wait_for(task, timeout=10)

    cmd = agent.resume_inputs[0]
    assert cmd.resume == {"intr-1": {"decisions": [{"type": "reject"}]}}


async def test_cancel_while_pending_finalizes_cancelled(
    settings_env, db_url, db, monkeypatch
) -> None:
    agent = _InterruptingAgent()
    _install(monkeypatch, agent)
    run_id = await _seed(db)
    bus = PostgresEventBus(poll_interval=0.1)

    task = asyncio.create_task(run_mod.run_agent(run_id, bus))
    await _wait_for_status(run_id, "pending_approval")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)

    async with sessionmaker()() as s:
        row = (
            await s.execute(
                text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)}
            )
        ).one()
    assert row.status == "cancelled"
    types = [p["type"] for p in await _event_payloads(run_id)]
    assert types[-1] == "run.ended"
