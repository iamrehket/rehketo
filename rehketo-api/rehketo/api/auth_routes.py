from __future__ import annotations

import contextlib
from typing import Annotated
from urllib.parse import quote, urlparse, urlunparse
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.auth import entra
from rehketo.auth import sessions as session_store
from rehketo.auth.cookies import (
    SESSION_COOKIE,
    clear_auth_cookies,
    set_csrf_cookie,
    set_session_cookie,
)
from rehketo.auth.csrf import issue_csrf_token
from rehketo.config import get_settings
from rehketo.core.logging import get_logger
from rehketo.db import get_session as db_session
from rehketo.db.models import Identity, User, UserRole

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

OAUTH_STATE_COOKIE = "rehketo_oauth_state"
OAUTH_VERIFIER_COOKIE = "rehketo_oauth_verifier"
OAUTH_NEXT_COOKIE = "rehketo_oauth_next"

# First printable ASCII byte; anything below is a C0 control char and is
# rejected from `?next=` paths (a control char can break out of a relative URL).
MIN_PRINTABLE_BYTE = 0x20


def _set_oauth_cookie(
    resp: RedirectResponse, name: str, value: str, *, secure: bool
) -> None:
    resp.set_cookie(
        name,
        value,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=600,
        path="/auth/",
    )


def _resolve_post_login_target(next_path: str | None) -> str:
    """Return an absolute URL on the UI origin.

    The scheme and host come *only* from the configured `ui_post_login_url`;
    `next_path` (when it passes `_is_safe_next`) supplies just the path
    component. Because the netloc is never derived from user input, the
    redirect can't be steered off-origin. Falls back to the configured
    post-login URL on anything suspicious.
    """
    if next_path and _is_safe_next(next_path):
        origin = urlparse(get_settings().ui_post_login_url)
        return urlunparse((origin.scheme, origin.netloc, next_path, "", "", ""))
    return get_settings().ui_post_login_url


def _is_safe_next(next_path: str) -> bool:
    if not next_path.startswith("/"):
        return False
    # Protocol-relative URLs (//evil.com), backslash trick, and control chars
    # can all turn a "next=/x" into off-origin navigation. Reject them.
    if next_path.startswith(("//", "/\\")):
        return False
    return all(ord(c) >= MIN_PRINTABLE_BYTE for c in next_path)


@router.get("/login")
async def login(
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    s = get_settings()
    start = entra.build_login()
    resp = RedirectResponse(start.authorize_url, status_code=302)
    _set_oauth_cookie(resp, OAUTH_STATE_COOKIE, start.state, secure=s.cookie_secure)
    _set_oauth_cookie(
        resp, OAUTH_VERIFIER_COOKIE, start.code_verifier, secure=s.cookie_secure
    )
    # Carry the caller's intended post-login path through the OAuth round
    # trip via a short-lived cookie. State-cookie-only preserves the flow
    # across the Entra hop without putting the path into the OAuth `state`
    # (which would leak it to the IdP logs).
    if next is not None and _is_safe_next(next):
        _set_oauth_cookie(resp, OAUTH_NEXT_COOKIE, next, secure=s.cookie_secure)
    return resp


def _oauth_error_redirect(exc: httpx.HTTPStatusError) -> RedirectResponse:
    code = "token_exchange_failed"
    with contextlib.suppress(ValueError):
        body = exc.response.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, str) and err:
                code = err
    logger.warning(
        "entra token exchange failed: status=%s code=%s",
        exc.response.status_code,
        code,
    )
    s = get_settings()
    sep = "&" if "?" in s.ui_post_login_url else "?"
    target = f"{s.ui_post_login_url}{sep}auth_error={quote(code)}"
    resp = RedirectResponse(target, status_code=302)
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth/")
    resp.delete_cookie(OAUTH_VERIFIER_COOKIE, path="/auth/")
    resp.delete_cookie(OAUTH_NEXT_COOKIE, path="/auth/")
    return resp


async def _upsert_user_and_identity(
    db: AsyncSession, claims: dict[str, object]
) -> User:
    subject = claims.get("oid") or claims.get("sub")
    if not subject:
        raise HTTPException(status_code=400, detail="token missing subject")
    subject_str = str(subject)

    existing = (
        await db.execute(
            select(Identity).where(
                Identity.provider == "entra",
                Identity.provider_subject == subject_str,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        user = (
            await db.execute(select(User).where(User.id == existing.user_id))
        ).scalar_one()
        return user

    name = claims.get("name")
    email = claims.get("email") or claims.get("preferred_username")
    user = User(
        id=uuid4(),
        display_name=str(name) if name else None,
        email=str(email) if email else None,
    )
    db.add(user)
    await db.flush()
    db.add(Identity(provider="entra", provider_subject=subject_str, user_id=user.id))
    db.add(UserRole(user_id=user.id, role="User"))
    await db.commit()
    return user


@router.get("/callback")
async def callback(
    db: Annotated[AsyncSession, Depends(db_session)],
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    rehketo_oauth_state: Annotated[str | None, Cookie(alias=OAUTH_STATE_COOKIE)] = None,
    rehketo_oauth_verifier: Annotated[
        str | None, Cookie(alias=OAUTH_VERIFIER_COOKIE)
    ] = None,
    rehketo_oauth_next: Annotated[str | None, Cookie(alias=OAUTH_NEXT_COOKIE)] = None,
) -> Response:
    if not rehketo_oauth_state or not rehketo_oauth_verifier:
        raise HTTPException(status_code=400, detail="missing oauth transient state")
    if state != rehketo_oauth_state:
        raise HTTPException(status_code=400, detail="oauth state mismatch")

    try:
        tokens = await entra.exchange_code_for_tokens(code, rehketo_oauth_verifier)
    except httpx.HTTPStatusError as exc:
        return _oauth_error_redirect(exc)
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise HTTPException(status_code=502, detail="no id_token in token response")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str):
        raise HTTPException(
            status_code=502, detail="no refresh_token in token response"
        )

    claims = entra.parse_id_token_claims(id_token)
    user = await _upsert_user_and_identity(db, claims)

    s = get_settings()
    session_id = await session_store.create_session(
        db,
        user_id=user.id,
        identity_provider="entra",
        refresh_token=refresh_token,
        ttl_minutes=s.session_ttl_minutes,
    )

    resp = RedirectResponse(
        _resolve_post_login_target(rehketo_oauth_next), status_code=302
    )
    ttl_seconds = s.session_ttl_minutes * 60
    set_session_cookie(resp, str(session_id), max_age_seconds=ttl_seconds)
    set_csrf_cookie(
        resp, issue_csrf_token(str(session_id)), max_age_seconds=ttl_seconds
    )
    # Clear the transient OAuth cookies (must match path="/auth/" set at login)
    resp.delete_cookie(OAUTH_STATE_COOKIE, path="/auth/")
    resp.delete_cookie(OAUTH_VERIFIER_COOKIE, path="/auth/")
    resp.delete_cookie(OAUTH_NEXT_COOKIE, path="/auth/")
    return resp


@router.post("/logout", status_code=204)
async def logout(
    db: Annotated[AsyncSession, Depends(db_session)],
    response: Response,
    rehketo_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    if rehketo_session:
        with contextlib.suppress(ValueError):
            await session_store.revoke_session(db, UUID(rehketo_session))
    clear_auth_cookies(response)
    return Response(status_code=204)


class DevLogin(BaseModel):
    email: str
    display_name: str | None = None
    roles: list[str] = []


@router.post("/devonly/login")
async def devonly_login(
    payload: DevLogin,
    db: Annotated[AsyncSession, Depends(db_session)],
    response: Response,
) -> dict[str, str]:
    s = get_settings()
    if not s.devonly_login_enabled:
        raise HTTPException(status_code=404, detail="not found")

    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    if existing is None:
        user = User(
            id=uuid4(),
            display_name=payload.display_name,
            email=payload.email,
        )
        db.add(user)
        await db.flush()
        db.add(
            Identity(
                provider="devonly",
                provider_subject=payload.email,
                user_id=user.id,
            )
        )
        await db.commit()
    else:
        user = existing

    # Replace roles
    await db.execute(sa_delete(UserRole).where(UserRole.user_id == user.id))
    for role in payload.roles:
        db.add(UserRole(user_id=user.id, role=role))
    await db.commit()

    session_id = await session_store.create_session(
        db,
        user_id=user.id,
        identity_provider="devonly",
        refresh_token="devonly",  # noqa: S106  # nosec B106  # devonly sentinel, not a real secret
        ttl_minutes=s.session_ttl_minutes,
    )
    ttl_seconds = s.session_ttl_minutes * 60
    set_session_cookie(response, str(session_id), max_age_seconds=ttl_seconds)
    set_csrf_cookie(
        response,
        issue_csrf_token(str(session_id)),
        max_age_seconds=ttl_seconds,
    )
    return {"user_id": str(user.id), "session_id": str(session_id)}
