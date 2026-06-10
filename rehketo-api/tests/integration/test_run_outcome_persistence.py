"""Integration tests — partial assistant text is persisted on cancel and fail,
and MessageOut surfaces run_status + run_error on reload.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessageChunk

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, Message, User, UserRole
from rehketo.main import create_app
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class _PartialThenHangAgent:
    """Yields one chunk of text, then blocks. Lets the test observe that a
    partial assistant text exists before cancellation fires."""

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        yield (
            AIMessageChunk(content="partial ", id="msg-p1"),
            {"langgraph_node": "agent"},
        )
        await asyncio.sleep(30)
        if False:
            yield  # pragma: no cover


async def _fake_build_agent(run_id: str) -> AsyncIterator[_PartialThenHangAgent]:
    yield _PartialThenHangAgent()


class _RaisingAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        yield (
            AIMessageChunk(content="partial-fail", id="msg-f1"),
            {"langgraph_node": "agent"},
        )
        raise RuntimeError("simulated LLM failure")


async def _fake_failing_build_agent(run_id: str) -> AsyncIterator[_RaisingAgent]:
    yield _RaisingAgent()


async def _no_title(_cid: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_cancel_persists_partial_assistant_text(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db.add_all([UserRole(user_id=u.id, role="User"), conv])  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    sid = await create_session(
        db,  # type: ignore[arg-type]
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "go"},
        )
        run_id = r.json()["run_id"]

        await asyncio.sleep(0.5)  # allow partial chunk to stream and be received

        r2 = await c.post(
            f"/runs/{run_id}/cancel",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
        )
        assert r2.status_code == 204

        await asyncio.sleep(2.0)  # let finalizer complete

        detail = await c.get(
            f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
        assert detail.status_code == 200
        msgs = detail.json()["messages"]

    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    a = assistant_msgs[0]
    assert a["content"]["text"] == "partial "
    assert a["run_id"] == run_id
    assert a["run_status"] == "cancelled"
    assert a["run_error"] is None


@pytest.mark.asyncio
async def test_fail_persists_partial_with_error(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_failing_build_agent)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db.add_all([UserRole(user_id=u.id, role="User"), conv])  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    sid = await create_session(
        db,  # type: ignore[arg-type]
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "go"},
        )
        run_id = r.json()["run_id"]

        # Consume the SSE stream so we know the run has terminated.
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
        ) as resp:
            async for _line in resp.aiter_lines():
                pass

        detail = await c.get(
            f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
        msgs = detail.json()["messages"]

    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    a = assistant_msgs[0]
    assert a["content"]["text"] == "partial-fail"
    assert a["run_status"] == "failed"
    assert a["run_error"] == {
        "code": "llm_failure",
        "message": "simulated LLM failure",
    }


@pytest.mark.asyncio
async def test_succeeded_run_message_has_succeeded_status(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check — succeeded runs also get run_status on MessageOut, as
    null (in-flight) vs. 'succeeded' (terminal)."""
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    class _HiAgent:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            yield (AIMessageChunk(content="ok", id="m1"), {"langgraph_node": "agent"})

    async def _build(run_id: str) -> AsyncIterator[_HiAgent]:
        yield _HiAgent()

    monkeypatch.setattr(run_mod, "build_agent", _build)
    monkeypatch.setattr(run_mod, "generate_title_if_needed", _no_title)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db.add_all([UserRole(user_id=u.id, role="User"), conv])  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    sid = await create_session(
        db,  # type: ignore[arg-type]
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))

    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "hi"},
        )
        run_id = r.json()["run_id"]

        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
        ) as resp:
            async for _line in resp.aiter_lines():
                pass

        detail = await c.get(
            f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)}
        )
        msgs = detail.json()["messages"]

    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["run_status"] == "succeeded"
    assert assistant["run_error"] is None


@pytest.mark.asyncio
async def test_user_message_has_no_run_status(
    settings_env: object, db_url: str, db: object
) -> None:
    u = User(id=uuid4(), display_name="A", email="a@x")
    db.add(u)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db.add_all([UserRole(user_id=u.id, role="User"), conv])  # type: ignore[union-attr]
    msg = Message(
        id=uuid4(),
        conversation_id=conv.id,
        role="user",
        content={"text": "hello"},
    )
    db.add(msg)  # type: ignore[union-attr]
    await db.commit()  # type: ignore[union-attr]

    sid = await create_session(
        db,  # type: ignore[arg-type]
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    user_msg = r.json()["messages"][0]
    assert user_msg["role"] == "user"
    assert user_msg["run_status"] is None
    assert user_msg["run_error"] is None
