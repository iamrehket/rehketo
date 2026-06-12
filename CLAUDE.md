<!-- GENERATED from AGENTS.md by tools/sync_agent_rules.py — do not edit by hand. -->

# CLAUDE.md

The canonical guide for this repo is **AGENTS.md**, reproduced below.

---

# Rehketo — AGENTS.md

The canonical guide for working anywhere in this repo. It governs both subprojects;
each has a nested `AGENTS.md` with stack-specific rules (`rehketo-api/AGENTS.md`,
`rehketo-ui/AGENTS.md`). Tools read the nearest file plus this one. If a rule here
conflicts with an ad-hoc task prompt, this document wins unless the prompt explicitly
overrides it.

Tool mirrors at `CLAUDE.md`, `.github/copilot-instructions.md`, and
`.cursor/rules/main.mdc` are generated from this file by `tools/sync_agent_rules.py`.
Edit here, never there.

## What it is

Rehketo is a minimal, self-hostable agent harness: a chat UI (`rehketo-ui`, SvelteKit)
and an API (`rehketo-api`, FastAPI) for talking to LLMs. The API runs agent "runs"
(deepagents + LangGraph) and streams them to the UI over SSE. Auth is cookie-session
based (Entra OIDC, Pattern B). In production the built UI is served *inside* the API
via `StaticFiles` — they ship together.

## What it is for

To be a harness you can point at different model providers, extend with tools, and
connect to your own services — without being locked to one vendor, one tool surface,
or one identity provider. It removes the per-project boilerplate of auth, streaming,
permissions, and agent orchestration.

## The north star

These are the project's goals. They are **direction**, tied to seams that already exist
in the code. Build toward them through those seams — but do **not** build an abstraction
before the second concrete case demands it (see charter rule 3).

- **Provider-agnostic.** Today the API talks to one Bifrost model alias
  (`claude-sonnet-4-6`) behind the `AGENT_MODEL` seam in `rehketo-api`. New providers
  attach at that seam; model/provider choice is config/data. Never hardcode a vendor in
  a handler or the UI.
- **MCP integration.** Not built yet. The first real tool lands behind a tool-registry
  seam; when MCP arrives, it registers tools through that same registry — not a parallel
  path.
- **OAuth connections.** Today Entra is the sign-in IdP. Downstream service connections
  (Google / GitHub / MS Graph) attach via the planned `connections` table + a consent
  route pair. Every new provider follows that one pattern.

## The charter

Numbered rules with teeth. Several are enforced by `tools/agent_guards.py`; the rest are
honored on review.

1. **No archaeology.** Code reads clearly without `git blame`. Comment the non-obvious
   *why*, not the *what*.
2. **Edit > create.** Extend an existing file unless a new responsibility needs its own.
3. **No premature abstraction.** Three concrete lines beat a wrong helper. Honor the
   North Star with *seams*, not speculative frameworks.
4. **No speculative error handling.** Validate at boundaries; trust internal invariants.
5. **Verify before "done."** Run the validation block and quote real output. "Done"
   without it violates this rule.
6. **No escape hatches without specifics.** `# type: ignore` / `# noqa` carry a code;
   `# pragma: no cover` carries a reason. (Enforced: `check-escape-hatches`.)
7. **Stay in scope.** A bug fix doesn't refactor; a feature doesn't touch unrelated tests.
8. **No orphan code.** No unused imports, dead branches, or skipped tests without a
   reference.

## How to validate work

Run the checks for the area you touched and **quote the real output** when you claim a
step passed.

Repo-wide (from root):
```bash
# `uv run --project rehketo-api python` pins the api's Python 3.14 — agent_guards
# uses ast.parse against the api source, so a 3.9 `python3` shim would crash.
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

rehketo-api (from `rehketo-api/`):
```bash
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run pytest
uv run pytest -m e2e   # offline browser suite; needs postgres up (just db). Opt-in by
                       # marker, so it silently rotted through the M3 `messages`→`items`
                       # rename — run it whenever you touch wire shapes or the UI flows.
uv run python ../tools/check_contract.py
```

rehketo-ui (from `rehketo-ui/`):
```bash
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

## Where things live

- `rehketo-api/` — FastAPI backend. See `rehketo-api/AGENTS.md`.
- `rehketo-ui/` — SvelteKit frontend. See `rehketo-ui/AGENTS.md`.
- `tools/` — repo guardrails (`agent_guards.py`), mirror generator
  (`sync_agent_rules.py`), contract check (`check_contract.py`), and their tests.
- `justfile` — local launch recipes (`just db`, `just api`, `just ui`,
  `just db-down`). One recipe per process; run each in its own terminal in the
  order the API README documents.
- `docs/superpowers/{specs,plans}/` — design specs and implementation plans.
- `AGENTS.md` + mirrors — this file and its generated copies.

## Conventions in force

- **Conventional Commits.** `feat|fix|chore|docs|style|refactor|perf|test|build|ci|revert`.
- **Stealth mode.** No AI attribution / `Co-Authored-By` trailers. (Enforced:
  `check-no-ai-attribution`.)
- **One responsibility per file.** Split rather than stuff.
- Stack-specific conventions live in the nested `AGENTS.md` files.

## Don'ts

- Don't hardcode a model/provider/IdP where a seam exists (North Star).
- Don't hand-edit `CLAUDE.md`, `.github/copilot-instructions.md`, or
  `.cursor/rules/main.mdc` — edit `AGENTS.md` and run `tools/sync_agent_rules.py`.
- Don't bypass hooks with `--no-verify`.
- Don't add a config knob or code path "for later." YAGNI (charter rule 3).
