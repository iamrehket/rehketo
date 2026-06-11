"""Which MCP servers a run may use. The single permission gate decides:
chat.use_mcp_server + the server row's allowed_roles as resource_roles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from rehketo.db.models import McpServer
from rehketo.permissions.dependencies import ResolvedPermissions

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

# Sentinel used when allowed_servers is called outside a user request (e.g.
# at run-build time where we have roles but no authenticated user_id).
_NO_USER_ID = UUID(int=0)


async def allowed_servers(db: AsyncSession, roles: Iterable[str]) -> list[McpServer]:
    # Build a ResolvedPermissions so the single permission gate is respected.
    perms = ResolvedPermissions(user_id=_NO_USER_ID, roles=frozenset(roles))
    rows = (
        (
            await db.execute(
                select(McpServer)
                .where(McpServer.enabled.is_(True))
                .order_by(McpServer.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        s
        for s in rows
        if perms.can(
            "chat.use_mcp_server",
            resource_type="mcp_server",
            resource_id=s.id,
            resource_roles=s.allowed_roles,
        )
    ]
