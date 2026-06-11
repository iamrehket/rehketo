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
