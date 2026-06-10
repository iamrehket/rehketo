"""OAuth `next=` preservation across the Entra round-trip.

The UI redirects signed-out users to `/login?next=<current path>`, the login
page appends `?next=...` to `/auth/login`, and the callback must honor it.
Without this round-trip, any signed-in user always lands on the default
post-login URL — the "Chrome took me to the wrong page initially" bug.

The `next` path is held server-side in the `oauth_pending_logins` table,
keyed by the state token, so no user-supplied value rides in a cookie.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from rehketo.auth.entra import authority
from rehketo.db.models import PendingLogin
from rehketo.main import create_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _fake_id_token() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"sub": "sub-n", "oid": "oid-n", "email": "n@example.com", "name": "N"}
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}."


def _token_response() -> dict[str, object]:
    return {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": _fake_id_token(),
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@pytest.mark.asyncio
async def test_login_persists_next_server_side_for_safe_path(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get("/auth/login", params={"next": "/c/abc-123"})
    assert r.status_code == 302
    # No user-supplied value rides in a cookie anymore.
    assert "rehketo_oauth_next=" not in r.headers.get("set-cookie", "")
    # The path is persisted server-side, keyed by the state token the browser got.
    state = r.cookies.get("rehketo_oauth_state")
    assert state
    row = (
        await db.execute(select(PendingLogin).where(PendingLogin.state == state))
    ).scalar_one()
    assert row.next_path == "/c/abc-123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "//evil.example.com/x",
        "http://evil.example.com/",
        "/\\evil",
        "no-leading-slash",
        "",
    ],
)
async def test_login_ignores_unsafe_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession, unsafe: str
) -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get("/auth/login", params={"next": unsafe})
    assert r.status_code == 302
    assert "rehketo_oauth_next=" not in r.headers.get("set-cookie", "")
    rows = (await db.execute(select(PendingLogin))).all()
    assert rows == []


@pytest.mark.asyncio
@respx.mock
async def test_callback_uses_pending_login_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    token_url = f"{authority()}/oauth2/v2.0/token"
    respx.post(token_url).mock(
        return_value=respx.MockResponse(200, json=_token_response())
    )
    db.add(
        PendingLogin(
            state="s1",
            next_path="/c/deep-link",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get(
            "/auth/callback",
            params={"code": "abc", "state": "s1"},
            cookies={"rehketo_oauth_state": "s1", "rehketo_oauth_verifier": "v1"},
        )
    assert r.status_code == 302
    assert r.headers["location"] == "http://127.0.0.1:5173/c/deep-link"
    # Single-use: the row is consumed.
    remaining = (await db.execute(select(PendingLogin))).all()
    assert remaining == []


@pytest.mark.asyncio
@respx.mock
async def test_login_callback_full_round_trip(
    settings_env: pytest.MonkeyPatch, db_url: str
) -> None:
    """Drive login's own generated state through to the callback: the state
    token login stores in the cookie must be the same key the callback consumes
    the pending-login row by."""
    token_url = f"{authority()}/oauth2/v2.0/token"
    respx.post(token_url).mock(
        return_value=respx.MockResponse(200, json=_token_response())
    )

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        login_resp = await c.get("/auth/login", params={"next": "/c/round-trip"})
        assert login_resp.status_code == 302
        state = c.cookies.get("rehketo_oauth_state")
        assert state

        callback_resp = await c.get(
            "/auth/callback", params={"code": "abc", "state": state}
        )

    assert callback_resp.status_code == 302
    assert callback_resp.headers["location"] == "http://127.0.0.1:5173/c/round-trip"


@pytest.mark.asyncio
@respx.mock
async def test_callback_rejects_unsafe_pending_next(
    settings_env: pytest.MonkeyPatch, db_url: str, db: AsyncSession
) -> None:
    # Defense in depth: even if an unsafe value were somehow stored, the
    # callback's resolver re-validates and falls back to the configured URL.
    token_url = f"{authority()}/oauth2/v2.0/token"
    respx.post(token_url).mock(
        return_value=respx.MockResponse(200, json=_token_response())
    )
    db.add(
        PendingLogin(
            state="s1",
            next_path="//evil.example.com/pwn",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db.commit()

    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get(
            "/auth/callback",
            params={"code": "abc", "state": "s1"},
            cookies={"rehketo_oauth_state": "s1", "rehketo_oauth_verifier": "v1"},
        )
    assert r.status_code == 302
    assert r.headers["location"].startswith("http://127.0.0.1:5173/")
    assert "evil.example.com" not in r.headers["location"]
