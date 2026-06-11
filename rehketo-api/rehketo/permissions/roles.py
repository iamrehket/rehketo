from rehketo.permissions.actions import ACTIONS

# v1 role-to-permission mapping. Migrates into OpenFGA at the ReBAC cutover.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "Admin": frozenset(ACTIONS),
    "Moderator": frozenset(
        {
            "chat.create_conversation",
            "chat.view_conversation",
            "chat.rename_conversation",
            "chat.delete_conversation",
            "chat.write",
            "chat.cancel_run",
            "chat.upload_files",
            "chat.use_mcp_server",
        }
    ),
    "User": frozenset(
        {
            "chat.create_conversation",
            "chat.view_conversation",
            "chat.rename_conversation",
            "chat.delete_conversation",
            "chat.write",
            "chat.cancel_run",
            "chat.upload_files",
            "chat.use_mcp_server",
        }
    ),
}
