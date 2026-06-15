"""Spike success metric: does wiring skills lift correct-capability use?

Not a gate — it prints baseline vs wired activation counts so the spike
checkpoint has evidence. Marked `eval` so it is opt-in like the e2e suite.
"""

from __future__ import annotations

import pytest

PROMPTS = [
    "What open PRs are on the repo?",
    "Summarize the latest pull request.",
    "Are there any code reviews waiting on me?",
    "What's our reimbursement deadline?",  # doc-skill case
    "How do I file a travel expense?",  # doc-skill case
]


@pytest.mark.eval
async def test_print_discovery_lift() -> None:
    # Implement by running run_agent twice over PROMPTS — once with skills
    # wiring disabled (monkeypatch resolve_skills to return empty) and once
    # enabled — counting runs whose run_events include a tool.call from the
    # expected server/skill. Print the two counts. Use the live bifrost stub
    # from Task 7 with non-scripted (real-model) turns if available; otherwise
    # document that this requires a live model and skip when BIFROST creds are
    # absent.
    pytest.skip("Run manually against a live model; see tests/eval/README.md")
