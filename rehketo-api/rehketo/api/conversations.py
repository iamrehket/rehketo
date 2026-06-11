from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.db.models import Conversation, Message, Run, RunEvent
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationOut(BaseModel):
    id: UUID


class ConversationSummary(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationSummary]


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: dict[str, object]
    run_id: UUID | None
    created_at: datetime
    # Terminal state of the linked run, joined from `runs`. Null when the
    # message has no run (user messages) or when the run is still in flight.
    # UI uses this to render 'cancelled' or 'failed' badges on reload without
    # replaying the SSE stream.
    run_status: str | None = None
    run_error: dict[str, object] | None = None


class MessageItem(MessageOut):
    kind: Literal["message"] = "message"


class ToolCallItem(BaseModel):
    """A tool invocation reconstructed from run_events on reload — the event
    log is the single source of truth for live streaming, resume, and
    transcript history. result is None while no tool.result event exists
    (in-flight, or the run died mid-call)."""

    kind: Literal["tool"] = "tool"
    run_id: UUID
    call_id: str
    tool: str
    arguments: dict[str, object]
    result: str | None = None
    is_error: bool | None = None
    created_at: datetime


TranscriptItem = Annotated[MessageItem | ToolCallItem, Field(discriminator="kind")]


class ConversationDetail(ConversationSummary):
    # Chronologically interleaved transcript — messages + tool activity
    # reconstructed from run_events. Replaces the old `messages` field.
    items: list[TranscriptItem]
    # In-flight run for this conversation (queued/running), newest first.
    # The UI uses this to reattach to the live SSE stream on open.
    # Best-effort: a run abandoned by a process crash stays queued/running
    # until the next startup sweep, so this can briefly point at a dead run —
    # the subscriber then just waits and the sweep's closure events end it.
    active_run_id: UUID | None = None


class ConversationPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _tool_items(db: AsyncSession, conversation_id: UUID) -> list[ToolCallItem]:
    """Reconstruct ToolCallItem pairs from run_events for a conversation.

    tool.call events seed an entry; the matching tool.result (same run_id +
    call_id) fills in result/is_error. Keying by (run_id, call_id) prevents
    collisions when provider-supplied IDs like "call_0" repeat across runs.
    Unmatched calls are left pending (result=None). Query is extracted here to
    keep get_conversation under the branch/statement caps.

    Growth note: the query filters every run_event row of the conversation
    through an unindexed JSONB type check; fine at present scale — when it
    bites, a partial index on tool event types or delta pruning in the startup
    sweep is the remedy.
    """
    rows = (
        await db.execute(
            select(RunEvent.run_id, RunEvent.payload, RunEvent.created_at)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                Run.conversation_id == conversation_id,
                RunEvent.payload["type"].astext.in_(["tool.call", "tool.result"]),
            )
            .order_by(RunEvent.run_id, RunEvent.sequence)
        )
    ).all()
    by_call_id: dict[tuple[UUID, str], ToolCallItem] = {}
    for run_id, payload, created_at in rows:
        call_id = str(payload.get("call_id", ""))
        key = (run_id, call_id)
        if payload["type"] == "tool.call":
            by_call_id[key] = ToolCallItem(
                run_id=run_id,
                call_id=call_id,
                tool=str(payload.get("tool", "")),
                arguments=payload.get("arguments") or {},
                created_at=created_at,
            )
        elif key in by_call_id:
            item = by_call_id[key]
            by_call_id[key] = item.model_copy(
                update={
                    "result": str(payload.get("result", "")),
                    "is_error": bool(payload.get("is_error", False)),
                }
            )
    return list(by_call_id.values())


@router.post("", status_code=201, response_model=ConversationOut)
async def create_conversation(
    payload: ConversationCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> ConversationOut:
    perms.require(
        "chat.create_conversation",
        resource_type="conversation",
        resource_id=None,
    )
    conv = Conversation(id=uuid4(), user_id=perms.user_id, title=payload.title)
    db.add(conv)
    await db.commit()
    return ConversationOut(id=conv.id)


@router.get("", response_model=ConversationList)
async def list_conversations(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
    include_archived: bool = False,
) -> ConversationList:
    perms.require(
        "chat.view_conversation",
        resource_type="conversation",
        resource_id=None,
    )
    stmt = select(Conversation).where(Conversation.user_id == perms.user_id)
    if not include_archived:
        stmt = stmt.where(Conversation.archived_at.is_(None))
    stmt = stmt.order_by(Conversation.updated_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return ConversationList(
        items=[
            ConversationSummary(
                id=r.id,
                title=r.title,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> ConversationDetail:
    perms.require(
        "chat.view_conversation",
        resource_type="conversation",
        resource_id=conversation_id,
    )
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == perms.user_id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = (
        await db.execute(
            select(Message, Run.status, Run.error)
            .outerjoin(Run, Run.id == Message.run_id)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at)
        )
    ).all()
    active_run_id = (
        await db.execute(
            select(Run.id)
            .where(
                Run.conversation_id == conv.id,
                Run.status.in_(["queued", "running"]),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # Treat in-flight runs (queued/running) as "no terminal status yet" on the
    # wire — the UI only uses run_status to render terminal-state badges.
    terminal = {"succeeded", "failed", "cancelled"}
    message_items: list[TranscriptItem] = [
        MessageItem(
            id=m.id,
            conversation_id=m.conversation_id,
            role=m.role,
            content=m.content,
            run_id=m.run_id,
            created_at=m.created_at,
            run_status=run_status if run_status in terminal else None,
            run_error=run_error if run_status in terminal else None,
        )
        for m, run_status, run_error in rows
    ]
    tool_items: list[TranscriptItem] = list(await _tool_items(db, conv.id))
    # sorted() is stable; tool.call always commits before the run's assistant
    # message is inserted, so wall-clock timestamps order the transcript correctly.
    items = sorted(message_items + tool_items, key=lambda i: i.created_at)
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        active_run_id=active_run_id,
        items=items,
    )


@router.patch("/{conversation_id}", response_model=ConversationSummary)
async def patch_conversation(
    conversation_id: UUID,
    payload: ConversationPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> ConversationSummary:
    perms.require(
        "chat.rename_conversation",
        resource_type="conversation",
        resource_id=conversation_id,
    )
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == perms.user_id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    if payload.title is not None:
        conv.title = payload.title
        conv.updated_at = datetime.now(UTC)
    if payload.archived is True and conv.archived_at is None:
        conv.archived_at = datetime.now(UTC)
    if payload.archived is False:
        conv.archived_at = None

    await db.commit()
    await db.refresh(conv)
    return ConversationSummary(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require(
        "chat.delete_conversation",
        resource_type="conversation",
        resource_id=conversation_id,
    )
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == perms.user_id,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if conv.archived_at is None:
        conv.archived_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=204)
