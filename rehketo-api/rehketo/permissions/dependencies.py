from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.auth.dependencies import AuthContext, resolve_session
from rehketo.db import get_session
from rehketo.db.models import UserRole
from rehketo.permissions.resolved import ResolvedPermissions as ResolvedPermissions


async def resolve_permissions(
    db: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[AuthContext, Depends(resolve_session)],
) -> ResolvedPermissions:
    stmt = select(UserRole.role).where(UserRole.user_id == ctx.user_id)
    roles = {row[0] for row in (await db.execute(stmt)).all()}
    return ResolvedPermissions(user_id=ctx.user_id, roles=frozenset(roles))
