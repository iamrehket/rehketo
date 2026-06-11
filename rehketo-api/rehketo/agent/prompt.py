"""System prompt assembly — the single seam where per-user context joins the
base prompt. Compaction (see the roadmap's event-gated items) will plug in
here too; until then this stays a pure function with no I/O."""

from __future__ import annotations

BASE_SYSTEM_PROMPT = "You are a helpful assistant."


def assemble_system_prompt(custom_instructions: str | None) -> str:
    if custom_instructions is None or not custom_instructions.strip():
        return BASE_SYSTEM_PROMPT
    return (
        f"{BASE_SYSTEM_PROMPT}\n\n## User instructions\n{custom_instructions.strip()}"
    )
