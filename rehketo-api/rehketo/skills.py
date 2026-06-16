"""Resolve which skills a run may offer. A skill is global (owner_user_id NULL,
role-gated) or user-owned; mcp-skills are additionally cross-checked against
allowed_servers so we never offer a card for a server the user cannot run.

This is the canonical skill-resolution seam, kept neutral (db + permissions +
the neutral ``rehketo.servers`` only — no mcp, no deepagents) so both the agent
run loop and the api can use it as a single source of truth. The deepagents
adapters that turn these rows into SKILL.md files and subagents live in
``rehketo.mcp.skills``, which builds on top of this."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from rehketo.db.models import Skill
from rehketo.permissions.resolved import ResolvedPermissions
from rehketo.servers import allowed_servers

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ResolvedSkills:
    doc: list[Skill]
    mcp: list[Skill]


async def resolve_skills(
    db: AsyncSession, *, user_id: UUID, roles: Iterable[str]
) -> ResolvedSkills:
    # Iterable[str] may be a one-shot generator; this function consumes roles
    # twice (perms + allowed_servers), so materialize once.
    roles = list(roles)
    perms = ResolvedPermissions(user_id=user_id, roles=frozenset(roles))
    rows = (
        (
            await db.execute(
                select(Skill)
                .where(
                    Skill.enabled.is_(True),
                    or_(
                        Skill.owner_user_id.is_(None),
                        Skill.owner_user_id == user_id,
                    ),
                )
                .order_by(Skill.name)
            )
        )
        .scalars()
        .all()
    )
    # Global skills are role-gated like servers; owned skills bypass the role
    # gate (ownership is its own grant). The same permission the chat path uses.
    visible = [
        s
        for s in rows
        if perms.owns(s.owner_user_id)
        # resource_type is dormant in v1 RBAC (check ignores it) but is kept
        # for the OpenFGA cutover; "skill" follows the singular-of-table-name
        # convention, as servers.py uses "mcp_server" for the mcp_servers table.
        or perms.can(
            "chat.use_mcp_server",
            resource_type="skill",
            resource_id=s.id,
            resource_roles=s.allowed_roles,
        )
    ]
    # Owned shadows global: when a user's own skill shares a name with a global
    # one, keep the owned row so exactly one card (SKILL.md file or subagent)
    # exists per name in a run — the path /skills/{name} and the subagent name
    # are both keyed by name and must not collide.
    by_name: dict[str, Skill] = {}
    for s in visible:
        current = by_name.get(s.name)
        if current is None or (
            current.owner_user_id is None and perms.owns(s.owner_user_id)
        ):
            by_name[s.name] = s
    # by_name preserves the query's Skill.name ASC order (each name inserted
    # once; a collision replaces in place), so no re-sort is needed.
    visible = list(by_name.values())
    allowed_ids = {
        srv.id for srv in await allowed_servers(db, user_id=user_id, roles=roles)
    }
    return ResolvedSkills(
        doc=[s for s in visible if s.kind == "doc"],
        mcp=[s for s in visible if s.kind == "mcp" and s.mcp_server_id in allowed_ids],
    )
