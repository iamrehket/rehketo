from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from rehketo.auth.cookies import SESSION_COOKIE
from rehketo.auth.sessions import create_session
from rehketo.db.models import Conversation, Message, Run, User, UserRole
from rehketo.main import create_app
from rehketo.runs.event_bus import PostgresEventBus


async def test_items_interleave_tool_activity(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()

    # User message, then tool events (durable bus), then assistant message —
    # the natural order of a tool-using run.
    db.add(
        Message(
            id=uuid4(),
            conversation_id=conv.id,
            role="user",
            content={"text": "hi"},
        )
    )
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {
            "type": "tool.call",
            "call_id": "c1",
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )
    await bus.publish(
        str(run.id),
        {
            "type": "tool.result",
            "call_id": "c1",
            "result": "echo: hi",
            "is_error": False,
        },
    )

    db.add(
        Message(
            id=uuid4(),
            conversation_id=conv.id,
            role="assistant",
            content={"text": "done"},
            run_id=run.id,
        )
    )
    await db.commit()

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    items = r.json()["items"]
    kinds = [(i["kind"], i.get("role") or i.get("tool")) for i in items]
    assert kinds == [
        ("message", "user"),
        ("tool", "testsrv__echo"),
        ("message", "assistant"),
    ]
    tool_item = items[1]
    assert tool_item["call_id"] == "c1"
    assert tool_item["arguments"] == {"text": "hi"}
    assert tool_item["result"] == "echo: hi"
    assert tool_item["is_error"] is False
    assert tool_item["run_id"] == str(run.id)


async def test_two_runs_same_call_id_no_collision(settings_env, db_url, db) -> None:
    """Two runs in one conversation each emit call_id "c1" — both tool items
    must appear in the transcript with their respective run_ids and payloads."""
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()

    run1 = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="claude-sonnet-4-6",
    )
    run2 = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="claude-sonnet-4-6",
    )
    db.add(run1)
    db.add(run2)
    await db.commit()

    bus = PostgresEventBus()
    # Run 1 — tool call + result
    await bus.publish(
        str(run1.id),
        {
            "type": "tool.call",
            "call_id": "c1",
            "tool": "svc__alpha",
            "arguments": {"x": 1},
        },
    )
    await bus.publish(
        str(run1.id),
        {
            "type": "tool.result",
            "call_id": "c1",
            "result": "alpha-ok",
            "is_error": False,
        },
    )
    # Run 2 — same call_id string, different tool
    await bus.publish(
        str(run2.id),
        {
            "type": "tool.call",
            "call_id": "c1",
            "tool": "svc__beta",
            "arguments": {"y": 2},
        },
    )
    await bus.publish(
        str(run2.id),
        {
            "type": "tool.result",
            "call_id": "c1",
            "result": "beta-ok",
            "is_error": False,
        },
    )

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    tool_items = [i for i in r.json()["items"] if i["kind"] == "tool"]
    assert len(tool_items) == 2

    by_run = {i["run_id"]: i for i in tool_items}
    assert str(run1.id) in by_run
    assert str(run2.id) in by_run

    item1 = by_run[str(run1.id)]
    assert item1["tool"] == "svc__alpha"
    assert item1["arguments"] == {"x": 1}
    assert item1["result"] == "alpha-ok"
    assert item1["call_id"] == "c1"

    item2 = by_run[str(run2.id)]
    assert item2["tool"] == "svc__beta"
    assert item2["arguments"] == {"y": 2}
    assert item2["result"] == "beta-ok"
    assert item2["call_id"] == "c1"

    # Chronological order: run1 events were inserted first
    run_ids_in_order = [i["run_id"] for i in tool_items]
    assert run_ids_in_order == [str(run1.id), str(run2.id)]


async def test_transcript_includes_approval_items(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="succeeded",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {
            "type": "tool.approval_required",
            "approval_id": "ap-1",
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )
    await bus.publish(
        str(run.id),
        {
            "type": "tool.approval_decision",
            "approval_id": "ap-1",
            "decision": "deny",
        },
    )

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    items = r.json()["items"]
    approval_items = [i for i in items if i["kind"] == "approval"]
    assert len(approval_items) == 1
    item = approval_items[0]
    assert item["approval_id"] == "ap-1"
    assert item["tool"] == "testsrv__echo"
    assert item["arguments"] == {"text": "hi"}
    assert item["decision"] == "deny"


async def test_transcript_pending_approval_has_null_decision(
    settings_env, db_url, db
) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="pending_approval",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {
            "type": "tool.approval_required",
            "approval_id": "ap-1",
            "tool": "testsrv__echo",
            "arguments": {"text": "hi"},
        },
    )

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    assert r.status_code == 200
    items = r.json()["items"]
    approval_items = [i for i in items if i["kind"] == "approval"]
    assert len(approval_items) == 1
    assert approval_items[0]["decision"] is None


async def test_call_without_result_is_pending(settings_env, db_url, db) -> None:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="failed",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    await db.commit()

    bus = PostgresEventBus()
    await bus.publish(
        str(run.id),
        {"type": "tool.call", "call_id": "c1", "tool": "t__x", "arguments": {}},
    )

    sid = await create_session(
        db,
        user_id=u.id,
        identity_provider="entra",
        refresh_token="rt",
        ttl_minutes=60,
    )
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/conversations/{conv.id}", cookies={SESSION_COOKIE: str(sid)})
    items = r.json()["items"]
    assert items[0]["kind"] == "tool"
    assert items[0]["result"] is None
    assert items[0]["is_error"] is None
