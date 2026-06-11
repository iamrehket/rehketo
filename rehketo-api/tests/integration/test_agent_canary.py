"""
Live-dependency canary for the agent graph builder.

Every other integration test patches `rehketo.agent.run.build_agent` with a
fake so the run orchestrator can be exercised without touching LangGraph's
postgres checkpointer or Bifrost. That's the right default — fast, offline,
deterministic — but it means "the real build_agent still works" has no
automated coverage.

This canary fills the gap without adding LLM cost:

- Calls the REAL `build_agent`, which constructs an `AsyncPostgresSaver`
  via the app's DSN-stripping rule.
- Reads state for a thread that doesn't exist. The read path is enough to
  surface any schema drift (missing checkpointer tables, renamed columns,
  bad SQL after a LangGraph upgrade).
- Never invokes the LLM — no Bifrost, no Anthropic.

Marked `live_deps` so it is skipped by default. Run explicitly when you
want to prove the agent can talk to the checkpointer:

    uv run pytest -m live_deps
"""

from __future__ import annotations

import pytest

from rehketo.agent.graph import build_agent
from rehketo.agent.prompt import BASE_SYSTEM_PROMPT

pytestmark = pytest.mark.live_deps


async def test_build_agent_reaches_checkpointer(
    settings_env: object, db_url: str
) -> None:
    async for graph in build_agent("canary-run", BASE_SYSTEM_PROMPT):
        # Reading state for a brand-new thread must return a StateSnapshot
        # (possibly empty) without error. If the checkpointer tables are
        # missing or drifted, psycopg raises UndefinedTable here.
        state = await graph.aget_state(
            {"configurable": {"thread_id": "canary-nonexistent-thread"}}
        )
        assert state is not None
        return
    msg = "build_agent yielded nothing"
    raise AssertionError(msg)
