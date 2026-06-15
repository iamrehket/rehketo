"""Integration test — second cancel during finalizer is absorbed by asyncio.shield().

Models ``test_run_cancel.py``. The additional guarantee checked here:
after ``run_agent`` enters the ``CancelledError`` branch, a *second* cancel
delivered while the shielded finalizer is running does not strand the run
in ``running`` status — the DB update + bus publish inside
``asyncio.shield(_finalize_cancel())`` both complete.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.auth.csrf import issue_csrf_token
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, User, UserRole
from rehketo.runs.registry import get_registry, reset_registry_for_tests
from tests.integration._helpers import await_run_terminal, live_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


class _NeverStreamingAgent:
    async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
        await asyncio.sleep(30)
        if False:
            yield  # pragma: no cover


async def _fake_build_agent(
    run_id: str,
    system_prompt: str,
    tools: Sequence[Any] = (),
    interrupt_on: Any = None,
    subagents: Any = None,
    skill_sources: Any = None,
) -> AsyncIterator[_NeverStreamingAgent]:
    yield _NeverStreamingAgent()


async def test_second_cancel_during_finalizer_still_cancels(
    settings_env: object,
    db_url: str,
    db: object,
    monkeypatch: object,
) -> None:
    db_session: AsyncSession = db  # type: ignore[assignment]
    mp: pytest.MonkeyPatch = monkeypatch  # type: ignore[assignment]

    reset_registry_for_tests()

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

        await asyncio.sleep(0.3)

        # First cancel — HTTP endpoint (delivered asynchronously via the
        # control channel).
        r2 = await c.post(
            f"/runs/{run_id}/cancel",
            cookies={SESSION_COOKIE: str(sid), CSRF_COOKIE: csrf},
            headers={CSRF_HEADER: csrf},
        )
        assert r2.status_code == 204

        # Second cancel — directly through the registry, racing both the
        # control-channel delivery and the shielded finalizer. Returns False
        # once the task has finished; True while the task is still settling.
        # Either outcome is acceptable: the invariant under test is that the
        # run still ends in 'cancelled'.
        get_registry().cancel(UUID(run_id))

        # Cancellation is asynchronous now, so poll until terminal instead
        # of sleeping.
        status = await await_run_terminal(
            c, run_id, cookies={SESSION_COOKIE: str(sid)}, timeout_s=10
        )

    assert status == "cancelled"
