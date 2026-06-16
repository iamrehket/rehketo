from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import McpServer
from rehketo.servers import allowed_servers


def _server(name: str, roles: list[str], *, enabled: bool = True) -> McpServer:
    return McpServer(
        id=uuid4(),
        name=name,
        url=f"https://{name}.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=roles,
        enabled=enabled,
    )


async def test_filters_by_role_and_enabled(settings_env, db_url, db) -> None:
    db.add_all(
        [
            _server("everyone", ["Admin", "Moderator", "User"]),
            _server("admins-only", ["Admin"]),
            _server("disabled", ["User"], enabled=False),
        ]
    )
    await db.commit()

    # user_id is identity metadata until the OpenFGA cutover; any value works for RBAC.
    user_servers = await allowed_servers(db, user_id=uuid4(), roles=["User"])
    assert [s.name for s in user_servers] == ["everyone"]

    admin_servers = await allowed_servers(db, user_id=uuid4(), roles=["Admin"])
    assert [s.name for s in admin_servers] == ["admins-only", "everyone"]


async def test_no_roles_means_no_servers(settings_env, db_url, db) -> None:
    db.add(_server("everyone", ["Admin", "Moderator", "User"]))
    await db.commit()
    assert await allowed_servers(db, user_id=uuid4(), roles=[]) == []
