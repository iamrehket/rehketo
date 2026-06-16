"""Adapt resolved skills onto deepagents' native primitives: doc-skills become
in-state SKILL.md files, mcp-skills become server-scoped subagents. The pure
resolution (which skills a run may offer) lives in the neutral
``rehketo.skills`` so the api can reuse it; these adapters stay here because
they pull in deepagents and the mcp toolset builder."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from deepagents.backends.utils import create_file_data

from rehketo.mcp.registry import build_run_toolset

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AsyncExitStack
    from uuid import UUID

    from rehketo.db.models import McpServer, Skill
    from rehketo.runs.event_bus import RunEventBus


SKILLS_ROOT = "/skills/"


def doc_skill_files(skills: list[Skill]) -> dict[str, Any]:
    """Render each doc-skill as a SKILL.md (YAML frontmatter + body) keyed by
    the path SkillsMiddleware scans. deepagents reads these from agent state
    when the files are passed on invoke, so the DB stays the source of truth.

    Each value is a complete deepagents ``FileData`` dict — built via the
    library's own ``create_file_data`` so the shape always matches what its
    backends expect. A bare string raises ``TypeError: string indices must be
    integers`` on read; an incomplete dict (missing the ``created_at`` /
    ``modified_at`` timestamps) raises ``KeyError: 'modified_at'`` in the slice
    paths that re-emit a file. Letting deepagents build it keeps us correct as
    that contract evolves."""
    files: dict[str, Any] = {}
    for s in skills:
        # JSON-encode the scalars: JSON is a valid YAML subset, so a name or
        # trigger containing ':', '"', or a newline can't break frontmatter
        # parsing. Users author triggers now, so this is load-bearing.
        frontmatter = (
            "---\n"
            f"name: {json.dumps(s.name)}\n"
            f"description: {json.dumps(s.trigger)}\n"
            "---\n"
        )
        body = f"{frontmatter}\n{s.instructions}"
        files[f"{SKILLS_ROOT}{s.name}/SKILL.md"] = create_file_data(body)
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
