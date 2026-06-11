from __future__ import annotations

from rehketo.agent.prompt import BASE_SYSTEM_PROMPT, assemble_system_prompt


def test_none_returns_base_prompt() -> None:
    assert assemble_system_prompt(None) == BASE_SYSTEM_PROMPT


def test_blank_returns_base_prompt() -> None:
    assert assemble_system_prompt("   \n") == BASE_SYSTEM_PROMPT


def test_instructions_appended_under_delimited_section() -> None:
    result = assemble_system_prompt("Always answer in haiku.")
    assert result.startswith(BASE_SYSTEM_PROMPT)
    assert "## User instructions" in result
    assert result.endswith("Always answer in haiku.")


def test_instructions_are_stripped() -> None:
    result = assemble_system_prompt("  Be terse.  \n")
    assert result == f"{BASE_SYSTEM_PROMPT}\n\n## User instructions\nBe terse."
