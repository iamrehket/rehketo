"""Integration test — run cancellation flow.

Monkey-patches ``rehketo.agent.run.build_agent`` to return a fake async
generator whose ``astream`` blocks on ``asyncio.sleep(30)``.  This
bypasses Bifrost and deepagents entirely, letting us exercise the run
orchestrator's CancelledError path cleanly.

The correct patch target is ``rehketo.agent.run.build_agent``.  ``run.py``
uses ``from rehketo.agent.graph import build_agent``, which binds the name
``build_agent`` in ``rehketo.agent.run``'s namespace.  Patching
``rehketo.agent.graph.build_agent`` would NOT intercept calls made by
``run_agent`` because the local binding is already resolved at import time.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserRole
from rehketo.runs.registry import reset_registry_for_tests
from tests.integration._helpers import await_run_terminal, live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


class _NeverStreamingAgent:
    """Fake agent whose astream blocks for 30 s — long enough for cancel to fire."""

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        await asyncio.sleep(30)
        # Unreachable; just satisfies the async generator protocol.
        if False:
            yield  # pragma: no cover


async def _fake_build_agent(
    run_id: str, system_prompt: str, tools: Sequence[Any] = ()
) -> AsyncIterator[_NeverStreamingAgent]:
    yield _NeverStreamingAgent()


async def test_cancel_transitions_run_to_cancelled(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: object,
) -> None:
    db_session: AsyncSession = db  # type: ignore[assignment]
    mp: pytest.MonkeyPatch = monkeypatch  # type: ignore[assignment]

    # Reset the singleton registry so tasks from other tests don't bleed in.
    reset_registry_for_tests()

    # Patch build_agent in run.py's namespace.  run.py does
    # `from rehketo.agent.graph import build_agent`, so the name
    # `build_agent` is bound in rehketo.agent.run — that is the correct
    # patch target.  Patching rehketo.agent.graph.build_agent would not
    # affect the already-imported reference.
    import rehketo.agent.run as run_mod

    mp.setattr(run_mod, "build_agent", _fake_build_agent)

    u = User(id=uuid4(), display_name="A", email="a@x")
    db_session.add(u)
    await db_session.commit()
    conv = Conversation(id=uuid4(), user_id=u.id, title="t")
    db_session.add_all([UserRole(user_id=u.id, role="User"), conv])
    await db_session.commit()
    sid = await create_session(
        db_session,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    csrf = issue_csrf_token(str(sid))

    # live_app: cancel propagation now rides the control channel, so the test
    # needs the RunControlListener that _lifespan/live_app starts.
    async with (
        live_app() as app,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c,
    ):
        r = await c.post(
            f"/conversations/{conv.id}/messages",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
            json={"content": "hang please"},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]

        # Let the run transition to "running" (needs one event-loop yield).
        await asyncio.sleep(0.3)

        r2 = await c.post(
            f"/runs/{run_id}/cancel",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
        )
        assert r2.status_code == 204

        # Cancellation is asynchronous now (NOTIFY -> listener -> task), so
        # poll until the run reaches a terminal state instead of sleeping.
        status = await await_run_terminal(
            c, run_id, cookies={SESSION_COOKIE: str(sid)}, timeout_s=10
        )

    assert status == "cancelled"
