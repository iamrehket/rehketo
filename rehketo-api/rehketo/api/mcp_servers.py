from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,  # noqa: TC002  # FastAPI needs runtime type for Depends()
)

from rehketo.api._validators import NAME_PATTERN, validate_roles
from rehketo.auth.crypto import encrypt_token
from rehketo.db import get_session
from rehketo.db.models import McpServer
from rehketo.permissions.dependencies import ResolvedPermissions, resolve_permissions

router = APIRouter(prefix="/admin/mcp-servers", tags=["admin"])


class McpServerCreate(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, max_length=64)
    url: HttpUrl
    auth_token: str | None = Field(default=None, min_length=1)
    allowed_roles: list[str]
    enabled: bool = True
    auto_approve: bool = False

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str]) -> list[str]:
        return validate_roles(v)


class McpServerPatch(BaseModel):
    # name is identity (tool prefix, unique key) — not patchable; recreate
    # instead. auth_token: absent = unchanged, null = clear (distinguished
    # via model_fields_set).
    url: HttpUrl | None = None
    auth_token: str | None = Field(default=None, min_length=1)
    allowed_roles: list[str] | None = None
    enabled: bool | None = None
    auto_approve: bool | None = None

    @field_validator("allowed_roles")
    @classmethod
    def roles_must_be_known(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return validate_roles(v)


class McpServerOut(BaseModel):
    id: UUID
    name: str
    url: str
    has_auth_token: bool
    allowed_roles: list[str]
    enabled: bool
    auto_approve: bool
    created_at: datetime
    updated_at: datetime


class McpServerList(BaseModel):
    items: list[McpServerOut]


def _to_out(s: McpServer) -> McpServerOut:
    return McpServerOut(
        id=s.id,
        name=s.name,
        url=s.url,
        has_auth_token=s.auth_token_ct is not None,
        allowed_roles=s.allowed_roles,
        enabled=s.enabled,
        auto_approve=s.auto_approve,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


async def _get_or_404(db: AsyncSession, server_id: UUID) -> McpServer:
    server = (
        await db.execute(select(McpServer).where(McpServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    return server


@router.get("", response_model=McpServerList)
async def list_servers(
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerList:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=None
    )
    rows = (
        (await db.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    )
    return McpServerList(items=[_to_out(s) for s in rows])


@router.post("", status_code=201, response_model=McpServerOut)
async def create_server(
    payload: McpServerCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerOut:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=None
    )
    dup = (
        await db.execute(select(McpServer.id).where(McpServer.name == payload.name))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="server name already exists")
    server = McpServer(
        id=uuid4(),
        name=payload.name,
        url=str(payload.url),
        auth_token_ct=(
            encrypt_token(payload.auth_token) if payload.auth_token else None
        ),
        allowed_roles=payload.allowed_roles,
        enabled=payload.enabled,
        auto_approve=payload.auto_approve,
    )
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return _to_out(server)


@router.patch("/{server_id}", response_model=McpServerOut)
async def patch_server(
    server_id: UUID,
    payload: McpServerPatch,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> McpServerOut:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=server_id
    )
    server = await _get_or_404(db, server_id)
    if payload.url is not None:
        server.url = str(payload.url)
    if "auth_token" in payload.model_fields_set:
        server.auth_token_ct = (
            encrypt_token(payload.auth_token) if payload.auth_token else None
        )
    if payload.allowed_roles is not None:
        server.allowed_roles = payload.allowed_roles
    if payload.enabled is not None:
        server.enabled = payload.enabled
    if payload.auto_approve is not None:
        server.auto_approve = payload.auto_approve
    server.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(server)
    return _to_out(server)


@router.delete("/{server_id}", status_code=204)
async def delete_server(
    server_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    perms: Annotated[ResolvedPermissions, Depends(resolve_permissions)],
) -> Response:
    perms.require(
        "admin.manage_mcp_servers", resource_type="mcp_server", resource_id=server_id
    )
    server = await _get_or_404(db, server_id)
    await db.delete(server)
    await db.commit()
    return Response(status_code=204)
