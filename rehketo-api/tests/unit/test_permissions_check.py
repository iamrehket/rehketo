from uuid import uuid4

import pytest

from rehketo.permissions.check import (
    PermissionError,
    check_permission,
    permissions_for_roles,
)


def test_admin_has_all_chat_actions():
    perms = permissions_for_roles({"Admin"})
    assert "chat.create_conversation" in perms
    assert "chat.delete_conversation" in perms
    assert "admin.manage_users" in perms


def test_user_has_basic_chat_actions():
    perms = permissions_for_roles({"User"})
    assert "chat.view_conversation" in perms
    assert "chat.write" in perms
    assert "admin.manage_users" not in perms


def test_check_permission_allows():
    assert check_permission(
        {"User"}, "chat.write", resource_type="conversation", resource_id=uuid4()
    )


def test_check_permission_denies():
    assert not check_permission(
        {"User"}, "admin.manage_users", resource_type="system", resource_id=None
    )


def test_check_permission_rejects_unknown_action():
    with pytest.raises(PermissionError):
        check_permission(
            {"User"}, "not.a.real.action", resource_type=None, resource_id=None
        )


def test_use_mcp_server_requires_role_intersection() -> None:
    assert check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=["User", "Admin"],
    )
    assert not check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=["Admin"],
    )
    assert not check_permission(
        ["User"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id="00000000-0000-0000-0000-000000000001",
        resource_roles=[],
    )


def test_resource_roles_does_not_bypass_action_grant() -> None:
    # A role named in resource_roles still needs the action itself.
    assert not check_permission(
        ["Guest"],
        "chat.use_mcp_server",
        resource_type="mcp_server",
        resource_id=None,
        resource_roles=["Guest"],
    )


def test_existing_actions_unaffected_by_default() -> None:
    assert check_permission(
        ["User"], "chat.write", resource_type="conversation", resource_id=None
    )


def test_admin_manage_mcp_servers_is_admin_only() -> None:
    assert check_permission(
        ["Admin"], "admin.manage_mcp_servers", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["User"], "admin.manage_mcp_servers", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["Moderator"],
        "admin.manage_mcp_servers",
        resource_type=None,
        resource_id=None,
    )


def test_manage_skills_is_admin_only() -> None:
    assert check_permission(
        ["Admin"], "admin.manage_skills", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["User"], "admin.manage_skills", resource_type=None, resource_id=None
    )
    assert not check_permission(
        ["Moderator"],
        "admin.manage_skills",
        resource_type=None,
        resource_id=None,
    )


def test_owns_true_only_for_caller_owned_resources() -> None:
    from rehketo.permissions.resolved import ResolvedPermissions

    me = uuid4()
    perms = ResolvedPermissions(user_id=me, roles=frozenset())
    assert perms.owns(me) is True
    assert perms.owns(uuid4()) is False  # another user
    assert perms.owns(None) is False  # global / unowned resource


def test_author_skill_granted_to_all_chat_roles() -> None:
    assert check_permission(
        ["User"], "chat.author_skill", resource_type=None, resource_id=None
    )
    assert check_permission(
        ["Moderator"], "chat.author_skill", resource_type=None, resource_id=None
    )
    assert check_permission(
        ["Admin"], "chat.author_skill", resource_type=None, resource_id=None
    )
