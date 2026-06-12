"""
Canonical action vocabulary. Any permission check anywhere in the codebase
must reference a name declared here. Adding a new action is a schema change
to the permission surface; think about it as a public API evolution.
"""

ACTIONS: tuple[str, ...] = (
    # Chat domain
    "chat.create_conversation",
    "chat.view_conversation",
    "chat.rename_conversation",
    "chat.delete_conversation",
    "chat.write",
    "chat.cancel_run",
    "chat.upload_files",
    "chat.use_mcp_server",
    "chat.approve_tool_call",
    # Admin domain
    "admin.manage_users",
    "admin.view_audit",
    "admin.manage_mcp_servers",
)

ACTIONS_SET = frozenset(ACTIONS)
