def test_models_import_and_define_expected_tables() -> None:
    from rehketo.db import models

    table_names = {t.name for t in models.Base.metadata.tables.values()}
    required = {
        "users",
        "identities",
        "sessions",
        "oauth_pending_logins",
        "connections",
        "user_roles",
        "conversations",
        "messages",
        "runs",
        "run_events",
    }
    assert required.issubset(table_names), required - table_names
