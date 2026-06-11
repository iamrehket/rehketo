from __future__ import annotations

from typing import Annotated
from uuid import (
    UUID,  # noqa: TC003  # used in Pydantic fields and FastAPI query params at runtime
)

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.db.models import User, UserPreferences
from rehketo.permissions.actions import ACTIONS
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(tags=["me"])


class MeOut(BaseModel):
    id: UUID
    display_name: str | None
    email: str | None
    roles: list[str]


class CapabilitiesOut(BaseModel):
    actions: list[str]


@router.get("/me", response_model=MeOut)
async def me(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MeOut:
    user = (await db.execute(select(User).where(User.id == perms.user_id))).scalar_one()
    return MeOut(
        id=user.id,
        display_name=user.display_name,
        email=user.email,
        roles=sorted(perms.roles),
    )


@router.get("/me/capabilities", response_model=CapabilitiesOut)
async def capabilities(
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
    resource_type: str | None = None,
    resource_id: UUID | None = None,
) -> CapabilitiesOut:
    allowed = [
        a
        for a in ACTIONS
        if perms.can(a, resource_type=resource_type, resource_id=resource_id)
    ]
    return CapabilitiesOut(actions=allowed)


class PreferencesOut(BaseModel):
    custom_instructions: str


class PreferencesIn(BaseModel):
    custom_instructions: str = Field(max_length=4000)


@router.get("/me/preferences", response_model=PreferencesOut)
async def get_preferences(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> PreferencesOut:
    prefs = (
        await db.execute(
            select(UserPreferences).where(UserPreferences.user_id == perms.user_id)
        )
    ).scalar_one_or_none()
    return PreferencesOut(
        custom_instructions=prefs.custom_instructions if prefs else ""
    )


@router.put("/me/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> PreferencesOut:
    stmt = (
        pg_insert(UserPreferences)
        .values(user_id=perms.user_id, custom_instructions=body.custom_instructions)
        .on_conflict_do_update(
            index_elements=[UserPreferences.user_id],
            set_={
                "custom_instructions": body.custom_instructions,
                "updated_at": func.now(),
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
    return PreferencesOut(custom_instructions=body.custom_instructions)
