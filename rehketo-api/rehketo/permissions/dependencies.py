from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.auth.dependencies import AuthContext, resolve_session
from rehketo.db import get_session
from rehketo.db.models import UserRole
from rehketo.permissions.check import check_permission

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class ResolvedPermissions:
    user_id: UUID
    roles: frozenset[str]

    def can(
        self,
        action: str,
        *,
        resource_type: str | None = None,
        resource_id: UUID | str | None = None,
        resource_roles: Iterable[str] | None = None,
    ) -> bool:
        return check_permission(
            self.roles,
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_roles=resource_roles,
        )

    def require(
        self,
        action: str,
        *,
        resource_type: str | None = None,
        resource_id: UUID | str | None = None,
        resource_roles: Iterable[str] | None = None,
    ) -> None:
        if not self.can(
            action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_roles=resource_roles,
        ):
            raise HTTPException(status_code=403, detail=f"denied: {action}")


async def resolve_permissions(
    db: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[AuthContext, Depends(resolve_session)],
) -> ResolvedPermissions:
    stmt = select(UserRole.role).where(UserRole.user_id == ctx.user_id)
    roles = {row[0] for row in (await db.execute(stmt)).all()}
    return ResolvedPermissions(user_id=ctx.user_id, roles=frozenset(roles))
