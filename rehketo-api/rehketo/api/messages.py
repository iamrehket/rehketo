from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.config import get_settings
from rehketo.db import get_session
from rehketo.db.models import Conversation, Message, Run
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions
from rehketo.runs.claim import notify_run_queued

router = APIRouter(prefix="/conversations", tags=["messages"])


class MessageCreate(BaseModel):
    content: str


class MessageKickoffOut(BaseModel):
    message_id: UUID
    run_id: UUID


@router.post(
    "/{conversation_id}/messages",
    status_code=202,
    response_model=MessageKickoffOut,
)
async def post_message(
    conversation_id: UUID,
    payload: MessageCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MessageKickoffOut:
    perms.require(
        "chat.write",
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

    settings = get_settings()
    message_id = uuid4()
    run_id = uuid4()

    db.add(
        Message(
            id=message_id,
            conversation_id=conv.id,
            role="user",
            content={"text": payload.content},
        )
    )
    db.add(
        Run(
            id=run_id,
            conversation_id=conv.id,
            user_id=perms.user_id,
            status="queued",
            model=settings.agent_model,
        )
    )
    conv.updated_at = datetime.now(UTC)
    await notify_run_queued(db, run_id)
    await db.commit()

    return MessageKickoffOut(message_id=message_id, run_id=run_id)
