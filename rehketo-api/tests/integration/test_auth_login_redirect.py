from __future__ import annotations

from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient

from rehketo.main import create_app


@pytest.mark.asyncio
async def test_login_redirects_to_entra(
    settings_env: pytest.MonkeyPatch, db_url: str
) -> None:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.get("/auth/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert urlparse(loc).hostname == "login.microsoftonline.com"
    assert "client_id=cid" in loc
    assert "redirect_uri=" in loc
    assert "code_challenge=" in loc
    cookie_header = r.headers.get("set-cookie", "")
    assert "rehketo_oauth_state" in cookie_header
