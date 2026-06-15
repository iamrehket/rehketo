"""Resolve which skills a run may offer, and adapt them onto deepagents'
native primitives. A skill is global (owner_user_id NULL, role-gated) or
user-owned; mcp-skills are additionally cross-checked against allowed_servers
so we never offer a card for a server the user cannot run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from rehketo.db.models import Skill
from rehketo.mcp.servers import allowed_servers
from rehketo.permissions.resolved import ResolvedPermissions

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
        if s.owner_user_id == user_id
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
    allowed_ids = {
        srv.id for srv in await allowed_servers(db, user_id=user_id, roles=roles)
    }
    return ResolvedSkills(
        doc=[s for s in visible if s.kind == "doc"],
        mcp=[s for s in visible if s.kind == "mcp" and s.mcp_server_id in allowed_ids],
    )


SKILLS_ROOT = "/skills/"


def doc_skill_files(skills: list[Skill]) -> dict[str, str]:
    """Render each doc-skill as a SKILL.md (YAML frontmatter + body) keyed by
    the path SkillsMiddleware scans. deepagents reads these from agent state
    when the files are passed on invoke, so the DB stays the source of truth."""
    files: dict[str, str] = {}
    for s in skills:
        frontmatter = f"---\nname: {s.name}\ndescription: {s.trigger}\n---\n"
        files[f"{SKILLS_ROOT}{s.name}/SKILL.md"] = f"{frontmatter}\n{s.instructions}"
    return files
