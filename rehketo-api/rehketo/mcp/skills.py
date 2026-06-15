"""Resolve which skills a run may offer, and adapt them onto deepagents'
native primitives. A skill is global (owner_user_id NULL, role-gated) or
user-owned; mcp-skills are additionally cross-checked against allowed_servers
so we never offer a card for a server the user cannot run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from rehketo.db.models import Skill
from rehketo.mcp.registry import build_run_toolset
from rehketo.mcp.servers import allowed_servers
from rehketo.permissions.resolved import ResolvedPermissions

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from contextlib import AsyncExitStack
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from rehketo.db.models import McpServer
    from rehketo.runs.event_bus import RunEventBus


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


def doc_skill_files(skills: list[Skill]) -> dict[str, dict[str, str]]:
    """Render each doc-skill as a SKILL.md (YAML frontmatter + body) keyed by
    the path SkillsMiddleware scans. deepagents reads these from agent state
    when the files are passed on invoke, so the DB stays the source of truth.

    Each value is a deepagents ``FileData`` dict, not a bare string — its
    StateBackend reads ``file_data["content"]`` and would raise
    ``TypeError: string indices must be integers`` on a plain string. ``content``
    and ``encoding`` are the required keys; timestamps are optional."""
    files: dict[str, dict[str, str]] = {}
    for s in skills:
        frontmatter = f"---\nname: {s.name}\ndescription: {s.trigger}\n---\n"
        body = f"{frontmatter}\n{s.instructions}"
        files[f"{SKILLS_ROOT}{s.name}/SKILL.md"] = {
            "content": body,
            "encoding": "utf-8",
        }
    return files


async def build_skill_subagents(
    stack: AsyncExitStack,
    mcp_skills: list[Skill],
    servers: Sequence[McpServer],
    *,
    run_id: str,
    bus: RunEventBus,
) -> list[dict[str, Any]]:
    """One SubAgent per mcp-skill, scoped to its server's tools. Reuses the
    existing toolset builder (clients live on `stack`); tools are grouped by
    the "<server>__" name prefix the adapter assigns, and each subagent carries
    its server's M3.5 interrupt_on subset so approval stays orthogonal."""
    by_id = {srv.id: srv for srv in servers}
    # One skill per server in v1, but nothing enforces that at the DB level and
    # bundle-skills (later) may map several skills to one server — dedup so we
    # open each server's client exactly once.
    seen_ids: set[UUID] = set()
    needed: list[McpServer] = []
    for skill in mcp_skills:
        sid = skill.mcp_server_id
        if sid is not None and sid in by_id and sid not in seen_ids:
            needed.append(by_id[sid])
            seen_ids.add(sid)
    tools, interrupt_on = await build_run_toolset(stack, needed, run_id=run_id, bus=bus)
    subagents: list[dict[str, Any]] = []
    for skill in mcp_skills:
        sid = skill.mcp_server_id
        server = by_id.get(sid) if sid is not None else None
        if server is None:  # caller omitted this skill's server, or no server id
            continue
        prefix = f"{server.name}__"
        skill_tools = [t for t in tools if t.name.startswith(prefix)]
        if not skill_tools:
            # Server was unreachable at connect time; build_run_toolset skips it
            # (a broken tool server must not take the run down). Drop the skill.
            continue
        spec: dict[str, Any] = {
            "name": skill.name,
            "description": skill.trigger,
            "system_prompt": skill.instructions
            or (
                "You are a specialized subagent. Engage your tools when: "
                f"{skill.trigger}"
            ),
            "tools": skill_tools,
        }
        sub_interrupts = {k: v for k, v in interrupt_on.items() if k.startswith(prefix)}
        if sub_interrupts:
            spec["interrupt_on"] = sub_interrupts
        subagents.append(spec)
    return subagents
