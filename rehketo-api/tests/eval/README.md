# Skill-discovery eval (M4.5 spike metric)

`test_skill_discovery_lift.py` measures whether wiring skills improves how
often the agent reaches for the right capability. It needs a live model
(real Bifrost), so it is `@pytest.mark.eval` and skipped by default.

Run manually:

    uv run pytest tests/eval -m eval -s   # -s to see the printed counts

By default it targets the live local Bifrost using the **real** `BIFROST_BASE_URL`
/ `BIFROST_API_KEY` / `AGENT_MODEL` from `rehketo-api/.env` (the `settings_env`
test fixture points the app at a mock Bifrost, so the eval reads the file the app
itself loads). Override any of them with `EVAL_BIFROST_BASE_URL` /
`EVAL_BIFROST_API_KEY` / `EVAL_AGENT_MODEL` to point at a different endpoint. The
test skips cleanly if no Bifrost is reachable.

Note: if Bifrost governance is enforcing virtual keys, `BIFROST_API_KEY` must be a
valid VK (see the governance settings — `enforce_governance_header` /
`enforce_auth_on_inference`).

## Reading the table

- `✓` — the skill's sentinel fact appeared in the final answer (capability reached).
- `[tool]` — a `github__*` MCP tool call fired during the run.

For **doc-skills**, `✓` is the discovery signal: OFF can't know the fact, ON should
read the `SKILL.md`. For **mcp-skills**, `[tool]` is the better signal — the tool
fires via subagent delegation, but the subagent summarizes its result so the exact
sentinel token often doesn't survive into the main agent's final answer. Numbers
vary run-to-run (real model); read the direction, not the exact count.

Record baseline-vs-wired counts in the spike checkpoint notes on the M4.5 branch.
