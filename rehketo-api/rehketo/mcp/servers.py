"""Which MCP servers a run may use. The single permission gate decides:
chat.use_mcp_server + the server row's allowed_roles as resource_roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from rehketo.db.models import McpServer
from rehketo.permissions.dependencies import ResolvedPermissions

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


async def allowed_servers(
    db: AsyncSession, *, user_id: UUID, roles: Iterable[str]
) -> list[McpServer]:
    # Build a ResolvedPermissions so the single permission gate is respected.
    perms = ResolvedPermissions(user_id=user_id, roles=frozenset(roles))
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
