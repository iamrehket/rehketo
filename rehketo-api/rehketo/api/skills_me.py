from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID  # noqa: TC003  # Pydantic field at runtime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.db import get_session
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions
from rehketo.skills import resolve_skills

if TYPE_CHECKING:
    from rehketo.db.models import Skill

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


def _to_out(s: Skill, *, user_id: UUID) -> MySkillOut:
    owned = s.owner_user_id == user_id
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


@router.get("/me/skills", response_model=MySkillList)
async def list_my_skills(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> MySkillList:
    resolved = await resolve_skills(db, user_id=perms.user_id, roles=perms.roles)
    rows = sorted([*resolved.doc, *resolved.mcp], key=lambda s: s.name)
    return MySkillList(items=[_to_out(s, user_id=perms.user_id) for s in rows])
