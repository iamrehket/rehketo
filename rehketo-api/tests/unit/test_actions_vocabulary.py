from rehketo.permissions.actions import ACTIONS


def test_vocabulary_is_nonempty():
    assert len(ACTIONS) >= 8


def test_vocabulary_is_unique():
    assert len(ACTIONS) == len(set(ACTIONS))


def test_all_dotted_lowercase():
    for a in ACTIONS:
        assert a == a.lower()
        assert "." in a
        ns, _, leaf = a.partition(".")
        assert ns.isidentifier() and leaf.isidentifier()


def test_contains_expected_actions():
    required = {
        "chat.create_conversation",
        "chat.view_conversation",
        "chat.rename_conversation",
        "chat.delete_conversation",
        "chat.write",
        "chat.cancel_run",
        "chat.upload_files",
        "chat.approve_tool_call",
        "admin.manage_users",
        "admin.view_audit",
    }
    assert required.issubset(set(ACTIONS))
