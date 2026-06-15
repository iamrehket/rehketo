"""Spike success metric: does wiring skills lift correct-capability use?

Not a gate — it drives a REAL model (live Bifrost) through `run_agent` over a
small prompt set twice, once with skills wiring OFF and once ON, and prints a
per-prompt table plus totals so the M4.5 checkpoint has evidence. Marked
`eval` so it is opt-in (deselected from the default run; see pyproject addopts).

Configurations compared
-----------------------
- **OFF** — `resolve_skills` is patched to return nothing. The github MCP
  server is still enabled+allowed, so its tool binds FLAT to the main agent
  (today's M3 behaviour). Doc-skill content is absent entirely. This is the
  fair baseline: the MCP capability is present with a good tool description,
  just without skill framing.
- **ON** — real `resolve_skills`. The github server is exposed as an mcp-skill
  (a `task` subagent the model delegates to) and the expense doc-skill is
  surfaced via SkillsMiddleware. This is the progressive-discovery surface.

Signal
------
Each skill hides a unique sentinel reachable ONLY through that capability (an
MCP tool result, or a fact in the doc-skill body). A run "reached" the
capability if its sentinel appears in the final answer. For mcp prompts we also
report whether a `github__*` tool.call fired. The interesting cells are the mcp
prompts OFF-vs-ON (does framing change usage) and the doc prompts (OFF is the
control that *cannot* know the fact; ON should discover and read the SKILL.md).

Run manually (needs a live model — see tests/eval/README.md):

    uv run pytest tests/eval -m eval -s

Bifrost target defaults to the app's own rehketo-api/.env (BIFROST_BASE_URL /
BIFROST_API_KEY / AGENT_MODEL); override with EVAL_BIFROST_BASE_URL /
EVAL_BIFROST_API_KEY / EVAL_AGENT_MODEL.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

import rehketo.agent.run as run_mod
from rehketo.db import sessionmaker
from rehketo.db.models import (
    Conversation,
    McpServer,
    Message,
    Run,
    Skill,
    User,
    UserRole,
)
from rehketo.mcp import registry
from rehketo.mcp.skills import ResolvedSkills
from rehketo.runs.event_bus import PostgresEventBus

pytestmark = pytest.mark.eval

# Per-run ceiling so a hung delegation can't stall the whole sweep.
_RUN_TIMEOUT_S = 180.0

# Sentinels are deliberately unusual tokens: their presence in an answer is
# unambiguous proof the model reached the backing capability, not coincidence.
_PR_SENTINEL = "PR-7731"
_DEADLINE_SENTINEL = "REIMB-19D"
_EXPENSE_SENTINEL = "TRVL-X9Q"

_PROMPTS: list[dict[str, str]] = [
    {
        "prompt": "What open pull requests are on the repo?",
        "kind": "mcp",
        "sentinel": _PR_SENTINEL,
    },
    {
        "prompt": "Summarize the latest open pull request.",
        "kind": "mcp",
        "sentinel": _PR_SENTINEL,
    },
    {
        "prompt": "Is there a pull request about authentication waiting?",
        "kind": "mcp",
        "sentinel": _PR_SENTINEL,
    },
    {
        "prompt": "What is our reimbursement filing deadline?",
        "kind": "doc",
        "sentinel": _DEADLINE_SENTINEL,
    },
    {
        "prompt": "How do I file a travel expense?",
        "kind": "doc",
        "sentinel": _EXPENSE_SENTINEL,
    },
]

_DOC_INSTRUCTIONS = (
    "# Expense policy\n\n"
    f"Reimbursement claims must be filed within the window coded {_DEADLINE_SENTINEL} "
    "after a trip ends; late claims are rejected.\n\n"
    "To file a travel expense, submit receipts through the portal token "
    f"{_EXPENSE_SENTINEL}.\n"
)


_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def _dotenv(name: str) -> str | None:
    """Read a value straight from rehketo-api/.env. The settings_env fixture
    overwrites os.environ with mock Bifrost values, so the app's real key isn't
    reachable via Settings here — go to the file the app itself loads."""
    if not _ENV_FILE.exists():
        return None
    for line in _ENV_FILE.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip() == name:
            return v.strip()
    return None


def _bifrost_target() -> tuple[str, str, str]:
    # Precedence: explicit EVAL_* override -> the app's own .env -> default.
    # Defaulting to .env means `pytest tests/eval -m eval -s` targets the live
    # local Bifrost with the real virtual key, no manual export needed.
    base = os.environ.get("EVAL_BIFROST_BASE_URL") or (
        _dotenv("BIFROST_BASE_URL") or "http://localhost:8088/v1"
    )
    key = os.environ.get("EVAL_BIFROST_API_KEY") or (
        _dotenv("BIFROST_API_KEY") or "dev-noop"
    )
    model = os.environ.get("EVAL_AGENT_MODEL") or (
        _dotenv("AGENT_MODEL") or "claude-sonnet-4-6"
    )
    return base, key, model


def _skip_if_bifrost_unreachable(base_url: str, api_key: str) -> None:
    """Skip (don't fail) when no live model is reachable — keeps the suite green
    for contributors without a local Bifrost. Any HTTP response counts as up."""
    try:
        httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
    except httpx.HTTPError as exc:
        pytest.skip(f"live Bifrost not reachable at {base_url}: {exc}")


async def _seed_user(db: Any) -> Any:
    u = User(id=uuid4(), display_name="Eval", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    await db.commit()
    return u.id


async def _seed_skill_fixtures(db: Any) -> None:
    """The github MCP server + its mcp-skill, and the expense doc-skill. Rows
    exist for BOTH configs; the OFF run simply ignores them via the patched
    resolver, so the only variable between configs is skill framing."""
    srv = McpServer(
        id=uuid4(),
        name="github",
        url="https://unused.example.com/mcp",
        auth_token_ct=None,
        allowed_roles=["User"],
        enabled=True,
        auto_approve=True,
    )
    db.add(srv)
    await db.commit()
    db.add_all(
        [
            Skill(
                id=uuid4(),
                name="github",
                trigger="Use for GitHub pull-request and code-review questions.",
                kind="mcp",
                mcp_server_id=srv.id,
                allowed_roles=["User"],
                enabled=True,
            ),
            Skill(
                id=uuid4(),
                name="expense-policy",
                trigger=(
                    "Use when answering questions about expense reimbursement, "
                    "travel-expense filing, or refund deadlines."
                ),
                kind="doc",
                instructions=_DOC_INSTRUCTIONS,
                allowed_roles=["User"],
                enabled=True,
            ),
        ]
    )
    await db.commit()


async def _run_prompt(db: Any, user_id: Any, prompt: str) -> Any:
    """Seed a conversation + user turn + queued run, drive run_agent, and return
    (answer_text, github_tool_called)."""
    conv = Conversation(id=uuid4(), user_id=user_id)
    db.add(conv)
    await db.commit()
    db.add(
        Message(
            id=uuid4(),
            conversation_id=conv.id,
            role="user",
            content={"text": prompt},
        )
    )
    _, _, model = _bifrost_target()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=user_id,
        status="queued",
        model=model,
    )
    db.add(run)
    await db.commit()

    bus = PostgresEventBus(poll_interval=0.1)
    async with asyncio.timeout(_RUN_TIMEOUT_S):
        # run_agent swallows its own failures (marks the run 'failed' and
        # returns); read the run row afterwards so a misconfig surfaces instead
        # of looking like the model simply declined to use the capability.
        await run_mod.run_agent(run.id, bus)

    async with sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == conv.id,
                        Message.role == "assistant",
                    )
                )
            )
            .scalars()
            .all()
        )
        # The answer row carries no 'channel' (thinking rows do); fall back to
        # the last row if a provider produced only narration.
        answer = next(
            (m for m in rows if "channel" not in (m.content or {})),
            rows[-1] if rows else None,
        )
        answer_text = (answer.content or {}).get("text", "") if answer else ""
        tool_called = (
            await s.execute(
                text(
                    "SELECT count(*) FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.call' "
                    "AND payload->>'tool' LIKE 'github__%'"
                ),
                {"rid": str(run.id)},
            )
        ).scalar_one()
        run_row = (
            await s.execute(select(Run.status, Run.error).where(Run.id == run.id))
        ).one()
    run_error = (
        (run_row.error or {}).get("message") if run_row.status == "failed" else None
    )
    return {
        "answer": answer_text,
        "tool_called": bool(tool_called),
        "status": run_row.status,
        "run_error": run_error,
    }


async def _sweep(
    db: Any,
    user_id: Any,
    label: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in _PROMPTS:
        try:
            run = await _run_prompt(db, user_id, spec["prompt"])
            reached = spec["sentinel"].lower() in run["answer"].lower()
            tool_called = run["tool_called"]
            # A failed run (e.g. Bifrost misconfig) is the error to surface;
            # otherwise the harness error is whatever exception escaped.
            error = run["run_error"] if run["status"] == "failed" else None
        except Exception as exc:  # eval harness: one bad run must not abort the sweep
            reached, tool_called, error = False, False, repr(exc)
        results.append(
            {
                "config": label,
                "prompt": spec["prompt"],
                "kind": spec["kind"],
                "reached": reached,
                "tool_called": tool_called,
                "error": error,
            }
        )
    return results


def _print_report(off: list[dict[str, Any]], on: list[dict[str, Any]]) -> None:
    def fmt(r: dict[str, Any]) -> str:
        mark = "✓" if r["reached"] else "·"
        tool = " [tool]" if r["tool_called"] else ""
        err = f" ERROR={r['error']}" if r["error"] else ""
        return f"{mark}{tool}{err}"

    print("\n\n=== Skill discovery lift (M4.5 spike metric) ===")
    print(f"{'kind':<5} {'OFF':<10} {'ON':<10}  prompt")
    for o, n in zip(off, on, strict=True):
        print(f"{o['kind']:<5} {fmt(o):<10} {fmt(n):<10}  {o['prompt']}")

    def reached(rs: list[dict[str, Any]], kind: str | None = None) -> int:
        return sum(r["reached"] for r in rs if kind is None or r["kind"] == kind)

    print(
        f"\nreached  OFF={reached(off)}/{len(off)}  ON={reached(on)}/{len(on)}"
        f"   (mcp: OFF={reached(off, 'mcp')} ON={reached(on, 'mcp')};"
        f" doc: OFF={reached(off, 'doc')} ON={reached(on, 'doc')})"
    )
    print("================================================\n")


@pytest.mark.eval
async def test_print_discovery_lift(
    settings_env: Any, db_url: str, db: Any, monkeypatch: Any
) -> None:
    base_url, api_key, model = _bifrost_target()
    _skip_if_bifrost_unreachable(base_url, api_key)

    # settings_env points at a mock Bifrost; redirect to the real one and rebuild
    # the cached settings so build_chat_model() hits the live model.
    monkeypatch.setenv("BIFROST_BASE_URL", base_url)
    monkeypatch.setenv("BIFROST_API_KEY", api_key)
    monkeypatch.setenv("AGENT_MODEL", model)
    from rehketo.config import get_settings

    get_settings.cache_clear()

    from fastmcp import Client, FastMCP

    server = FastMCP("github")

    @server.tool
    def list_open_prs() -> str:
        """List the repository's currently open pull requests with their IDs."""
        return f"Open PRs: [{_PR_SENTINEL}] migrate auth to OIDC; [PR-7732] bump deps"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    user_id = await _seed_user(db)
    await _seed_skill_fixtures(db)

    # OFF: wiring disabled — github stays a flat main-agent tool, doc absent.
    async def _no_skills(*_a: Any, **_k: Any) -> ResolvedSkills:
        return ResolvedSkills(doc=[], mcp=[])

    monkeypatch.setattr(run_mod, "resolve_skills", _no_skills)
    off = await _sweep(db, user_id, "OFF")

    # ON: real resolution — github as a subagent skill, expense as a doc-skill.
    monkeypatch.undo()  # restore resolve_skills (and the env, re-applied below)
    monkeypatch.setenv("BIFROST_BASE_URL", base_url)
    monkeypatch.setenv("BIFROST_API_KEY", api_key)
    monkeypatch.setenv("AGENT_MODEL", model)
    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))
    get_settings.cache_clear()
    on = await _sweep(db, user_id, "ON")

    _print_report(off, on)

    # Harness sanity (NOT a lift gate): every run produced a record.
    assert len(off) == len(_PROMPTS)
    assert len(on) == len(_PROMPTS)
