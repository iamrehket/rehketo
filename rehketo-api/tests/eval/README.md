# Skill-discovery eval (M4.5 spike metric)

`test_skill_discovery_lift.py` measures whether wiring skills improves how
often the agent reaches for the right capability. It needs a live model
(real Bifrost), so it is `@pytest.mark.eval` and skipped by default.

Run manually:

    uv run pytest tests/eval -m eval -s   # -s to see the printed counts

Record baseline-vs-wired counts in the spike checkpoint notes on the M4.5
branch. A meaningful lift (e.g. the doc/MCP prompts trigger the skill when
wired and do not when flat) clears the spike's bar.
