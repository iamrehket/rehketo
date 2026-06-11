"""
End-to-end smoke: devonly login -> /me -> create conversation -> list ->
detail -> rename -> soft-delete -> verify list excludes archived -> logout ->
verify authorized route rejects.

Single cycle; all routes touched in order to prove Plan 1's slice is wired
end-to-end.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from rehketo.main import create_app


async def test_full_lifecycle(settings_env, db_url) -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Login via devonly
        r = await c.post(
            "/auth/devonly/login",
            json={"email": "al@example.com", "roles": ["User"]},
        )
        assert r.status_code == 200

        sid: str | None = None
        csrf: str | None = None
        for cookie in r.cookies.jar:
            if cookie.name == SESSION_COOKIE:
                sid = cookie.value
            if cookie.name == CSRF_COOKIE:
                csrf = cookie.value
        assert sid is not None
        assert csrf is not None

        auth_cookies = {SESSION_COOKIE: sid, CSRF_COOKIE: csrf}
        auth_headers = {CSRF_HEADER: csrf}

        # /me reports the session owner + User role
        r = await c.get("/me", cookies=auth_cookies)
        assert r.status_code == 200
        assert "User" in r.json()["roles"]

        # Create
        r = await c.post(
            "/conversations",
            json={},
            cookies=auth_cookies,
            headers=auth_headers,
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]

        # List shows the new conversation
        r = await c.get("/conversations", cookies=auth_cookies)
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

        # Detail shows the conversation and empty messages
        r = await c.get(f"/conversations/{conv_id}", cookies=auth_cookies)
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == conv_id
        assert body["items"] == []

        # Rename via PATCH
        r = await c.patch(
            f"/conversations/{conv_id}",
            json={"title": "named"},
            cookies=auth_cookies,
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert r.json()["title"] == "named"

        # Soft-delete via DELETE
        r = await c.delete(
            f"/conversations/{conv_id}",
            cookies=auth_cookies,
            headers=auth_headers,
        )
        assert r.status_code == 204

        # List now excludes the archived conversation
        r = await c.get("/conversations", cookies=auth_cookies)
        assert r.status_code == 200
        assert r.json()["items"] == []

        # Logout revokes the session (CSRF-enforced)
        r = await c.post("/auth/logout", cookies=auth_cookies, headers=auth_headers)
        assert r.status_code == 204

        # Subsequent authorized call rejected
        r = await c.get("/me", cookies=auth_cookies)
        assert r.status_code == 401
