"""Integration test — SSE GETs bypass CSRF middleware.

``CSRFMiddleware`` enforces token verification only on unsafe HTTP methods
(POST, PUT, PATCH, DELETE).  GET requests — including the SSE event stream
at ``GET /runs/{id}/events`` — must succeed without any CSRF cookie or header.

This test verifies that behaviour: it POSTs a message (with CSRF) to create a
run, then opens the SSE stream without supplying a CSRF token.  A 200 response
proves that the middleware passes the GET through unchallenged.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserRole
from rehketo.main import create_app
from rehketo.runs.registry import reset_registry_for_tests

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class _ImmediateAgent:
    """Fake agent that yields nothing — run completes immediately."""

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        if False:
            yield  # pragma: no cover


async def _fake_build_agent(
    run_id: str, system_prompt: str
) -> AsyncIterator[_ImmediateAgent]:
    yield _ImmediateAgent()


@pytest.mark.asyncio
async def test_sse_subscribe_does_not_require_csrf(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_registry_for_tests()

    import rehketo.agent.run as run_mod

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

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
            json={"content": "hello"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        # Let the run start so the SSE stream has something to return.
        await asyncio.sleep(0.1)

        # Open SSE without any CSRF cookie or header — only the session cookie.
        async with c.stream(
            "GET",
            f"/runs/{run_id}/events",
            cookies={SESSION_COOKIE: str(sid)},
            # Deliberately omit CSRF_COOKIE and CSRF_HEADER
        ) as resp:
            # A 200 here confirms GET bypasses CSRF middleware.
            assert resp.status_code == 200
