"""
The single permission gate. v1 implements role-based permissions;
at the OpenFGA cutover only this module's body changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rehketo.permissions.actions import ACTIONS_SET
from rehketo.permissions.roles import ROLE_PERMISSIONS

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID


class PermissionError(ValueError):
    pass


def permissions_for_roles(roles: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for r in roles:
        result |= ROLE_PERMISSIONS.get(r, frozenset())
    return frozenset(result)


def check_permission(
    roles: Iterable[str],
    action: str,
    *,
    resource_type: str | None,
    resource_id: UUID | str | None,
    resource_roles: Iterable[str] | None = None,
) -> bool:
    """
    Returns True iff the caller is allowed to perform `action` on the
    given resource. `resource_type` and `resource_id` are accepted now;
    v1 RBAC ignores them. Do not remove them from call sites.

    `resource_roles` is the per-resource role allowlist (today: an MCP
    server's allowed_roles). When provided, the caller must hold the
    action AND share at least one role with the allowlist. At the OpenFGA
    cutover this becomes a relationship check; only this body changes.
    """
    if action not in ACTIONS_SET:
        raise PermissionError(f"unknown action: {action!r}")
    caller_roles = set(roles)
    if action not in permissions_for_roles(caller_roles):
        return False
    if resource_roles is not None:
        return bool(caller_roles & set(resource_roles))
    return True
