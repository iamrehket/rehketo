from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Annotated
from uuid import UUID  # noqa: TC003  # used at runtime in Pydantic model + route path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)
from sse_starlette.sse import EventSourceResponse

from rehketo.db import get_session
from rehketo.db.models import Run
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions
from rehketo.runs.cancellation import TERMINAL_RUN_STATES, request_cancel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter(prefix="/runs", tags=["runs"])


class RunOut(BaseModel):
    id: UUID
    conversation_id: UUID
    status: str
    model: str


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    run_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> RunOut:
    perms.require(
        "chat.view_conversation",
        resource_type="run",
        resource_id=run_id,
    )
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        status=run.status,
        model=run.model,
    )


@router.get("/{run_id}/events")
async def run_events(
    run_id: UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
    from_sequence: int | None = None,
) -> EventSourceResponse:
    perms.require(
        "chat.view_conversation",
        resource_type="run",
        resource_id=run_id,
    )
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    bus = request.app.state.event_bus

    async def _stream() -> AsyncIterator[dict[str, str]]:
        # aclosing makes the subscription's cleanup run when this generator
        # exits, instead of whenever GC finalizes it.
        async with contextlib.aclosing(
            bus.subscribe(str(run_id), from_sequence=from_sequence)
        ) as events:
            async for event in events:
                yield _encode_sse_event(event)
                # The agent publishes run.ended as the last event on every
                # terminal path (succeeded / failed / cancelled). run.status
                # alone is NOT a stream terminator — succeeded in particular
                # fires before title generation, so closing on it would drop
                # the subsequent conversation.updated.
                if event.get("type") == "run.ended":
                    return

    return EventSourceResponse(_stream())


def _encode_sse_event(event: dict[str, object]) -> dict[str, str]:
    # sse-starlette stringifies dict `data` via str() (producing Python repr
    # with single quotes), so encode to JSON ourselves to keep the wire format
    # parseable. default=str handles datetime and similar non-JSON natives.
    return {
        "event": str(event["type"]),
        "data": json.dumps(event, default=str),
    }


@router.post("/{run_id}/cancel", status_code=204)
async def cancel_run(
    run_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> None:
    perms.require(
        "chat.cancel_run",
        resource_type="run",
        resource_id=run_id,
    )
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in TERMINAL_RUN_STATES:
        raise HTTPException(status_code=409, detail=f"run already {run.status}")
    if not await request_cancel(db, run_id):
        # Run went terminal between the check above and the UPDATE.
        raise HTTPException(status_code=409, detail="run already terminal")
