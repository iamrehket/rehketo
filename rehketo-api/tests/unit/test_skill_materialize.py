from __future__ import annotations

from uuid import uuid4

from rehketo.db.models import Skill
from rehketo.mcp.skills import SKILLS_ROOT, doc_skill_files


def _doc(name: str, trigger: str, body: str) -> Skill:
    return Skill(
        id=uuid4(),
        name=name,
        trigger=trigger,
        kind="doc",
        instructions=body,
        allowed_roles=["User"],
        enabled=True,
    )


def test_emits_skill_md_per_skill() -> None:
    files = doc_skill_files([_doc("policy", "reimburse", "# Policy\nbody")])
    assert list(files) == [f"{SKILLS_ROOT}policy/SKILL.md"]
    content = files[f"{SKILLS_ROOT}policy/SKILL.md"]
    assert content.startswith("---\n")
    assert "name: policy" in content
    assert "description: reimburse" in content
    assert content.rstrip().endswith("body")


def test_empty_when_no_docs() -> None:
    assert doc_skill_files([]) == {}
