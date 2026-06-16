from __future__ import annotations

import json
from uuid import uuid4

from deepagents.backends.utils import file_data_to_string

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
    # Values are deepagents FileData dicts ({"content","encoding"}), not bare
    # strings — its StateBackend indexes file_data["content"].
    file_data = files[f"{SKILLS_ROOT}policy/SKILL.md"]
    assert file_data["encoding"] == "utf-8"
    content = file_data["content"]
    assert content.startswith("---\n")
    assert 'name: "policy"' in content
    assert 'description: "reimburse"' in content
    assert content.rstrip().endswith("body")


def test_empty_when_no_docs() -> None:
    assert doc_skill_files([]) == {}


def test_trigger_with_yaml_metacharacters_round_trips() -> None:
    """A user-authored trigger with a colon, quote, or newline must not break
    SkillsMiddleware frontmatter parsing. JSON-encoding the scalar guarantees a
    well-formed YAML string."""
    hostile = 'use when: he said "hi"\nand more'
    files = doc_skill_files([_doc("policy", hostile, "body")])
    file_data = files[f"{SKILLS_ROOT}policy/SKILL.md"]
    text = file_data_to_string(file_data)  # type: ignore[arg-type]
    # frontmatter block is exactly two lines (name + description), so two newlines
    front = text.split("---\n")[1]
    assert front.count("\n") == 2  # name line + description line
    # the description scalar is a single JSON-quoted token (no raw newline/colon leaks)
    assert json.dumps(hostile) in text


def test_values_satisfy_deepagents_filedata_contract() -> None:
    """Guards the seam that shipped broken: SkillsMiddleware reads each file via
    file_data_to_string -> file_data["content"], which raises
    ``TypeError: string indices must be integers`` on a bare string. The unit
    test above and the mcp-only live test never exercised this, so a doc-skill
    crashed the run at SkillsMiddleware.before_agent until the FileData fix."""
    files = doc_skill_files([_doc("policy", "reimburse", "# Policy\nbody")])
    for file_data in files.values():
        # Must not raise; must round-trip the rendered SKILL.md body.
        text = file_data_to_string(file_data)  # type: ignore[arg-type]  # plain dict satisfies the FileData TypedDict at runtime
        assert text.startswith("---\n")
        assert text.rstrip().endswith("body")
