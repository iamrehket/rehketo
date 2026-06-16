from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.api._validators import NAME_PATTERN
from rehketo.db import get_session
from rehketo.db.models import Skill
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions
from rehketo.skills import resolve_skills

router = APIRouter(tags=["me"])


class MySkillOut(BaseModel):
    id: UUID
    name: str
    display_name: str | None
    kind: str
    trigger: str
    instructions: str | None
    enabled: bool
    source: str  # 'global' | 'owned'
    editable: bool


class MySkillList(BaseModel):
    items: list[MySkillOut]


class MySkillCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    trigger: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(min_length=1)
    enabled: bool = True


class MySkillPatch(BaseModel):
    # name + kind are identity — not patchable. enabled toggles inline.
    display_name: str | None = Field(default=None, max_length=128)
    trigger: str | None = Field(default=None, min_length=1, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None


def _to_out(s: Skill, *, perms: ResolvedPermissions) -> MySkillOut:
    owned = perms.owns(s.owner_user_id)
    return MySkillOut(
        id=s.id,
        name=s.name,
        display_name=s.display_name,
        kind=s.kind,
        trigger=s.trigger,
        instructions=s.instructions,
        enabled=s.enabled,
        source="owned" if owned else "global",
        editable=owned and s.kind == "doc",
    )


async def _get_owned_doc_or_404(
    db: AsyncSession, skill_id: UUID, perms: ResolvedPermissions
) -> Skill:
    s = (
        await db.execute(
            select(Skill).where(and_(Skill.id == skill_id, Skill.kind == "doc"))
        )
    ).scalar_one_or_none()
    if s is None or not perms.owns(s.owner_user_id):
        raise HTTPException(status_code=404, detail="skill not found")
    return s


@router.get("/me/skills", response_model=MySkillList)
async def list_my_skills(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillList:
    resolved = await resolve_skills(db, user_id=perms.user_id, roles=perms.roles)
    rows = sorted([*resolved.doc, *resolved.mcp], key=lambda s: s.name)
    return MySkillList(items=[_to_out(s, perms=perms) for s in rows])


@router.post("/me/skills", status_code=201, response_model=MySkillOut)
async def create_my_skill(
    payload: MySkillCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillOut:
    perms.require("chat.author_skill", resource_type="skill", resource_id=None)
    dup = (
        await db.execute(
            select(Skill.id).where(
                and_(Skill.owner_user_id == perms.user_id, Skill.name == payload.name)
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="skill name already exists")
    skill = Skill(
        id=uuid4(),
        name=payload.name,
        display_name=payload.display_name,
        trigger=payload.trigger,
        kind="doc",
        instructions=payload.instructions,
        owner_user_id=perms.user_id,
        allowed_roles=[],
        enabled=payload.enabled,
    )
    db.add(skill)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Race backstop: concurrent create slips past the SELECT pre-check above
        # and hits the partial unique index — surface as clean 409, not 500.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="skill name already exists"
        ) from exc
    await db.refresh(skill)
    return _to_out(skill, perms=perms)


@router.patch("/me/skills/{skill_id}", response_model=MySkillOut)
async def patch_my_skill(
    skill_id: UUID,
    payload: MySkillPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillOut:
    perms.require("chat.author_skill", resource_type="skill", resource_id=skill_id)
    skill = await _get_owned_doc_or_404(db, skill_id, perms)
    if "display_name" in payload.model_fields_set:
        skill.display_name = payload.display_name
    if payload.trigger is not None:
        skill.trigger = payload.trigger
    if payload.instructions is not None:
        skill.instructions = payload.instructions
    if payload.enabled is not None:
        skill.enabled = payload.enabled
    skill.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill, perms=perms)


@router.delete("/me/skills/{skill_id}", status_code=204)
async def delete_my_skill(
    skill_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require("chat.author_skill", resource_type="skill", resource_id=skill_id)
    skill = await _get_owned_doc_or_404(db, skill_id, perms)
    await db.delete(skill)
    await db.commit()
    return Response(status_code=204)
