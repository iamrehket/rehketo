"""Shared test helpers for integration tests.

Not auto-collected by pytest (underscore prefix). Use these to reduce
boilerplate in tests that need a logged-in user + conversation + a fake
streaming agent that bypasses Bifrost.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from langchain_core.messages import AIMessageChunk

from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, Run, User, UserRole
from rehketo.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from fastapi import FastAPI
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


# --- App with a started event bus -------------------------------------------


@asynccontextmanager
async def live_app() -> AsyncIterator[FastAPI]:
    """create_app() with its event bus + control listener started, like
    _lifespan does.

    httpx's ASGITransport never runs lifespan, and PostgresEventBus delivers
    live wakes only from the LISTEN task started there — without it a
    subscriber sees new events on the slow fallback re-poll, which loses to
    drain_sse's timeout. The control listener is what makes POST /cancel
    actually cancel the local task. Use this in any test that consumes SSE
    while the run is still streaming, or cancels a run.
    """
    app = create_app()
    await app.state.event_bus.start()
    await app.state.control_listener.start()
    try:
        yield app
    finally:
        await app.state.control_listener.stop()
        await app.state.event_bus.stop()


# --- DB seed -----------------------------------------------------------------


async def mk_running_run() -> str:
    """Create user + conversation + a Run in status='running'; return run id."""
    async with sessionmaker()() as db:
        user = User(id=uuid4(), display_name="t", email=f"{uuid4().hex}@example.test")
        conv = Conversation(id=uuid4(), user_id=user.id)
        run = Run(
            id=uuid4(),
            conversation_id=conv.id,
            user_id=user.id,
            status="running",
            model="test-model",
        )
        # No relationship() on these models, so SQLAlchemy won't order the
        # inserts by FK — flush parent rows before their children.
        db.add(user)
        await db.flush()
        db.add(conv)
        await db.flush()
        db.add(run)
        await db.commit()
        return str(run.id)


async def seed_user_and_conv(
    db: AsyncSession,
    *,
    ttl_minutes: int = 60,
    roles: Sequence[str] = ("User",),
    email: str = "test@example.com",
) -> tuple[User, Conversation, UUID, str]:
    """Create user + conv + session; return (user, conv, session_id, csrf_token)."""
    user = User(id=uuid4(), display_name="Test", email=email)
    db.add(user)
    await db.flush()
    conv = Conversation(id=uuid4(), user_id=user.id, title="t")
    db.add(conv)
    for role in roles:
        db.add(UserRole(user_id=user.id, role=role))
    await db.commit()
    sid = await create_session(
        db,
        user_id=user.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=ttl_minutes,
    )
    csrf = issue_csrf_token(str(sid))
    return user, conv, sid, csrf


# --- Fake streaming agent ----------------------------------------------------


class FakeStreamingAgent:
    """Yields a fixed sequence of AIMessageChunk content strings.

    Optionally sleeps between chunks (`delay_s`) or raises after the Nth chunk
    (`raise_after_chunks=N`). All three scenarios in
    test_run_ended_terminal_guarantee.py use this with different parameters.
    """

    def __init__(
        self,
        chunks: Sequence[str] = ("hel", "lo"),
        *,
        delay_s: float = 0.0,
        raise_after_chunks: int | None = None,
    ) -> None:
        self.chunks = tuple(chunks)
        self.delay_s = delay_s
        self.raise_after_chunks = raise_after_chunks

    async def astream(self, *_args: Any, **_kwargs: Any) -> AsyncGenerator[Any]:
        # stream_mode='messages' yields (AIMessageChunk, metadata) tuples.
        for i, content in enumerate(self.chunks):
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            else:
                await asyncio.sleep(0)
            yield (
                AIMessageChunk(content=content, id="msg-fake-1"),
                {"langgraph_node": "agent"},
            )
            if self.raise_after_chunks is not None and i + 1 >= self.raise_after_chunks:
                raise RuntimeError("fake agent boom")


def make_fake_build_agent(agent: FakeStreamingAgent) -> Any:
    """Build a coroutine usable as a drop-in replacement for `build_agent`.

    `run.py` does `from rehketo.agent.graph import build_agent` then
    `async for agent in build_agent(...): ...` — i.e. it iterates an async
    generator. So our replacement must also be an async generator.
    """

    async def _build(
        run_id: str, system_prompt: str, tools: Sequence[Any] = ()
    ) -> AsyncIterator[FakeStreamingAgent]:
        yield agent

    return _build


# --- SSE helpers -------------------------------------------------------------


async def drain_sse(
    client: AsyncClient,
    run_id: str,
    cookies: dict[str, str],
    *,
    timeout_s: float = 5.0,
) -> list[dict[str, Any]]:
    """Consume the SSE stream for a run until `run.ended` (or timeout).

    Returns the full ordered list of decoded `data:` payloads. Tests then
    assert on types and ordering. Uses `asyncio.timeout` so a missing
    run.ended (the exact bug class we're guarding) surfaces as a failed
    test rather than a hang.
    """
    events: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(timeout_s):
            async with client.stream(
                "GET",
                f"/runs/{run_id}/events",
                cookies=cookies,
            ) as resp:
                if resp.status_code != 200:
                    raise AssertionError(
                        f"SSE returned {resp.status_code}, expected 200"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    events.append(json.loads(line[6:]))
                    if events[-1].get("type") == "run.ended":
                        return events
    except TimeoutError:
        # Fall through — the test sees the partial list and can assert
        # what's missing.
        pass
    return events


async def await_run_terminal(
    client: AsyncClient,
    run_id: str,
    cookies: dict[str, str],
    *,
    timeout_s: float = 5.0,
) -> str:
    """Poll GET /runs/{id} until status is succeeded|failed|cancelled.

    Useful when a test doesn't need every SSE event — just to know the
    background task finished before the next assertion.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/runs/{run_id}", cookies=cookies)
        if r.status_code == 200:
            status = r.json().get("status")
            if status in {"succeeded", "failed", "cancelled"}:
                return str(status)
        await asyncio.sleep(0.02)
    raise TimeoutError(f"run {run_id} did not reach a terminal state")
