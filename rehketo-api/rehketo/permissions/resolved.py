"""Permission value object usable from both HTTP handlers and background run tasks.

rehketo.permissions.dependencies is its HTTP constructor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException

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
