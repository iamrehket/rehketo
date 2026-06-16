from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.db.models import McpServer, Skill
from rehketo.permissions.check import known_roles
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(prefix="/admin/skills", tags=["admin"])

_NAME_PATTERN = r"^[a-z0-9]+([_-][a-z0-9]+)*$"
_KNOWN_ROLES: frozenset[str] = known_roles()


def _validate_roles(roles: list[str]) -> list[str]:
    unknown = sorted(set(roles) - _KNOWN_ROLES)
    if unknown:
        raise ValueError(f"unknown role(s): {', '.join(unknown)}")
    return roles


class AdminSkillCreate(BaseModel):
    name: str = Field(pattern=_NAME_PATTERN, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    kind: str
    trigger: str = Field(min_length=1, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1)
    mcp_server_id: UUID | None = None
    allowed_roles: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str]) -> list[str]:
        return _validate_roles(v)

    @model_validator(mode="after")
    def kind_backing(self) -> AdminSkillCreate:
        # Mirror the DB skills_kind_backing check at the boundary so a bad shape
        # is a clean 422, not a 500 from the failed INSERT.
        if self.kind == "doc":
            if not self.instructions or self.mcp_server_id is not None:
                raise ValueError("doc skills require instructions and no mcp_server_id")
        elif self.kind == "mcp":
            if self.mcp_server_id is None or self.instructions is not None:
                raise ValueError("mcp skills require mcp_server_id and no instructions")
        else:
            raise ValueError("kind must be 'doc' or 'mcp'")
        return self


class AdminSkillPatch(BaseModel):
    # name + kind are identity — not patchable.
    display_name: str | None = None
    trigger: str | None = Field(default=None, min_length=1, max_length=2000)
    instructions: str | None = Field(default=None, min_length=1)
    mcp_server_id: UUID | None = None
    allowed_roles: list[str] | None = None
    enabled: bool | None = None

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _validate_roles(v)


class AdminSkillOut(BaseModel):
    id: UUID
    name: str
    display_name: str | None
    kind: str
    trigger: str
    instructions: str | None
    mcp_server_id: UUID | None
    owner_user_id: UUID | None
    allowed_roles: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AdminSkillList(BaseModel):
    items: list[AdminSkillOut]


def _to_out(s: Skill) -> AdminSkillOut:
    return AdminSkillOut(
        id=s.id,
        name=s.name,
        display_name=s.display_name,
        kind=s.kind,
        trigger=s.trigger,
        instructions=s.instructions,
        mcp_server_id=s.mcp_server_id,
        owner_user_id=s.owner_user_id,
        allowed_roles=s.allowed_roles,
        enabled=s.enabled,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _get_or_404(db: AsyncSession, skill_id: UUID) -> Skill:
    # Global-only surface; user skills live behind /me/skills.
    s = (
        await db.execute(
            select(Skill).where(
                and_(Skill.id == skill_id, Skill.owner_user_id.is_(None))
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return s


@router.get("", response_model=AdminSkillList)
async def list_skills(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillList:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=None)
    rows = (
        (
            await db.execute(
                select(Skill).where(Skill.owner_user_id.is_(None)).order_by(Skill.name)
            )
        )
        .scalars()
        .all()
    )
    return AdminSkillList(items=[_to_out(s) for s in rows])


@router.post("", status_code=201, response_model=AdminSkillOut)
async def create_skill(
    payload: AdminSkillCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillOut:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=None)
    if payload.mcp_server_id is not None:
        srv = (
            await db.execute(
                select(McpServer.id).where(McpServer.id == payload.mcp_server_id)
            )
        ).scalar_one_or_none()
        if srv is None:
            raise HTTPException(status_code=400, detail="mcp_server_id does not exist")
    # Global namespace (owner_user_id IS NULL).
    dup = (
        await db.execute(
            select(Skill.id).where(
                and_(Skill.owner_user_id.is_(None), Skill.name == payload.name)
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="skill name already exists")
    skill = Skill(
        id=uuid4(),
        name=payload.name,
        display_name=payload.display_name,
        kind=payload.kind,
        trigger=payload.trigger,
        instructions=payload.instructions,
        mcp_server_id=payload.mcp_server_id,
        owner_user_id=None,
        allowed_roles=payload.allowed_roles,
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
    return _to_out(skill)


@router.patch("/{skill_id}", response_model=AdminSkillOut)
async def patch_skill(
    skill_id: UUID,
    payload: AdminSkillPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> AdminSkillOut:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=skill_id)
    skill = await _get_or_404(db, skill_id)
    if "display_name" in payload.model_fields_set:
        skill.display_name = payload.display_name
    if payload.trigger is not None:
        skill.trigger = payload.trigger
    if "instructions" in payload.model_fields_set and skill.kind == "doc":
        if not payload.instructions:
            raise HTTPException(
                status_code=422, detail="doc skills require non-empty instructions"
            )
        skill.instructions = payload.instructions
    if payload.mcp_server_id is not None and skill.kind == "mcp":
        # An mcp-skill never clears its server; a doc-skill PATCH carrying
        # mcp_server_id is ignored because kind is immutable.
        srv = (
            await db.execute(
                select(McpServer.id).where(McpServer.id == payload.mcp_server_id)
            )
        ).scalar_one_or_none()
        if srv is None:
            raise HTTPException(status_code=400, detail="mcp_server_id does not exist")
        skill.mcp_server_id = payload.mcp_server_id
    if payload.allowed_roles is not None:
        skill.allowed_roles = payload.allowed_roles
    if payload.enabled is not None:
        skill.enabled = payload.enabled
    skill.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(skill)
    return _to_out(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require("admin.manage_skills", resource_type="skill", resource_id=skill_id)
    skill = await _get_or_404(db, skill_id)
    await db.delete(skill)
    await db.commit()
    return Response(status_code=204)
