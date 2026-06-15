from __future__ import annotations

from typing import Any

import rehketo.agent.graph as graph_mod


async def test_build_agent_forwards_skills_and_subagents(
    settings_env, monkeypatch
) -> None:
    # build_agent reads get_settings() twice (build_chat_model + _checkpointer_dsn);
    # settings_env supplies the env so this stays a pure unit test that doesn't
    # depend on a local .env (which CI lacks).
    captured: dict[str, Any] = {}

    def _fake_create_deep_agent(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "AGENT"

    class _NullSaver:
        async def __aenter__(self) -> _NullSaver:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(graph_mod, "create_deep_agent", _fake_create_deep_agent)
    monkeypatch.setattr(
        graph_mod.AsyncPostgresSaver,
        "from_conn_string",
        lambda dsn: _NullSaver(),
    )

    subs = [{"name": "github", "description": "repos", "tools": []}]
    async for agent in graph_mod.build_agent(
        "run-1", "sys", subagents=subs, skill_sources=["/skills/"]
    ):
        assert agent == "AGENT"
    assert captured["subagents"] == subs
    assert captured["skills"] == ["/skills/"]
