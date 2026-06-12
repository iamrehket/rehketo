# Thinking/Answer Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop MCP tool output from leaking into the assistant bubble, and separate the agent's intermediate narration ("thinking") from its final answer — live and on reload.

**Architecture:** Filter the LangGraph delta stream to AI messages only (tool traffic already has its own `tool.call`/`tool.result` events). Track AI turns as segments keyed by `message_id`; persist intermediate turns as assistant messages with `{"channel": "thinking"}` and the final turn as the plain-`{text}` answer. The UI folds finished segments into local thinking items as it streams, and a pure `groupTranscript` function collapses a run's thinking + tool + approval items into a collapsible WorkingBlock.

**Tech Stack:** FastAPI + LangGraph/deepagents (api), SvelteKit + Svelte 5 runes + Vitest (ui).

**Spec:** `docs/superpowers/specs/2026-06-12-thinking-separation-design.md`. Two mechanism refinements vs. the spec's wording (same behavior, correct ordering):

1. A thinking segment's persisted `created_at` is its **last-delta time**, not the time the boundary was *detected*. The boundary is only detectable when the *next* segment's first delta arrives — which is after the tool rows were written, so stamping at detection time would sort the narration *after* its tool call. The last token of a turn always precedes the adapter's `tool.call` publish, so last-delta time interleaves correctly.
2. The UI keeps only the **current** segment in streaming state (`streamingText` + `streamingMessageId`) and folds a finished segment into a local thinking transcript item the moment a `tool.call`/`tool.approval_required`/new-`message_id` delta proves the turn ended. Rendering is identical to the spec's "segment list" description; arrival-order folding is what keeps narration above its tool chip.

**Pre-flight:** API checks run from `rehketo-api/`, UI checks from `rehketo-ui/`. Integration tests need the dev database (`just db` if not already running). Commits: Conventional Commits, **no AI attribution trailers**.

---

### Task 1: Filter non-AI chunks out of the delta stream (api)

The root bug: `stream_mode="messages"` yields `ToolMessage`s (raw MCP output) and `transform_chunk` streams them as `message.delta`.

**Files:**
- Modify: `rehketo-api/rehketo/agent/events.py`
- Test: `rehketo-api/tests/unit/test_agent_events_transform.py`

- [ ] **Step 1: Rewrite the transform tests with real message classes and add the filter test**

Replace the entire body of `rehketo-api/tests/unit/test_agent_events_transform.py` with:

```python
from __future__ import annotations

from langchain_core.messages import AIMessageChunk, ToolMessage

from rehketo.agent.events import transform_chunk


def test_message_delta_chunk_emits_message_delta() -> None:
    chunk = AIMessageChunk(content="hello ", id="msg-1")
    events = list(transform_chunk((chunk, {"langgraph_node": "agent"})))
    assert len(events) == 1
    assert events[0]["type"] == "message.delta"
    assert events[0]["message_id"] == "msg-1"
    assert events[0]["delta"] == "hello "


def test_empty_chunk_emits_nothing() -> None:
    assert list(transform_chunk((AIMessageChunk(content="", id="msg-1"), {}))) == []


def test_tool_message_emits_nothing() -> None:
    """ToolMessages carry raw MCP output; the adapter already publishes them
    as tool.call/tool.result. Streaming them as deltas is the clobbering bug."""
    msg = ToolMessage(content="raw tool output", tool_call_id="call_0", id="t-1")
    assert list(transform_chunk((msg, {}))) == []


def test_list_of_text_blocks_is_concatenated() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ],
        id="msg-2",
    )
    events = list(transform_chunk((chunk, {})))
    assert len(events) == 1
    assert events[0]["delta"] == "hello world"
    assert events[0]["message_id"] == "msg-2"


def test_list_of_plain_strings_is_concatenated() -> None:
    chunk = AIMessageChunk(content=["foo", "bar"], id="msg-3")
    events = list(transform_chunk((chunk, {})))
    assert events[0]["delta"] == "foobar"


def test_list_with_non_text_blocks_is_skipped() -> None:
    chunk = AIMessageChunk(
        content=[
            {"type": "tool_use", "id": "t1", "input": {}, "name": "x"},
            {"type": "text", "text": "after tool"},
        ],
        id="msg-4",
    )
    events = list(transform_chunk((chunk, {})))
    assert events[0]["delta"] == "after tool"


def test_empty_list_content_emits_nothing() -> None:
    assert list(transform_chunk((AIMessageChunk(content=[], id="msg-5"), {}))) == []
```

- [ ] **Step 2: Run the tests to verify the new one fails**

Run: `cd rehketo-api && uv run pytest tests/unit/test_agent_events_transform.py -v`
Expected: `test_tool_message_emits_nothing` FAILS (the ToolMessage content streams through); the rest PASS.

- [ ] **Step 3: Add the isinstance filter to transform_chunk**

In `rehketo-api/rehketo/agent/events.py`, add the import and the type check. `AIMessageChunk` subclasses `AIMessage`, so one check covers both:

```python
from langchain_core.messages import AIMessage
```

(top-level import, after `from typing import ...`), and change `transform_chunk` to:

```python
def transform_chunk(chunk: tuple[Any, dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Convert a LangGraph `stream_mode='messages'` chunk into zero or more
    events in our stable schema. Yields nothing for empty / metadata-only
    chunks — and for non-AI messages: `stream_mode='messages'` also yields
    ToolMessages (raw tool output), which the MCP adapter already publishes
    as tool.call/tool.result events."""
    msg, _metadata = chunk
    if not isinstance(msg, AIMessage):
        return
    raw = msg.content
    delta = _stringify_content(raw)
    if not delta:
        return
    yield {
        "type": "message.delta",
        "message_id": msg.id,
        "delta": delta,
    }
```

(The `getattr` calls become direct attribute access — the isinstance check guarantees them.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd rehketo-api && uv run pytest tests/unit/test_agent_events_transform.py -v`
Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/agent/events.py rehketo-api/tests/unit/test_agent_events_transform.py
git commit -m "fix(api): stop streaming ToolMessage content as message.delta"
```

---

### Task 2: SegmentTracker (api)

New single-responsibility module: turn the delta stream into ordered segments.

**Files:**
- Create: `rehketo-api/rehketo/agent/segments.py`
- Test: `rehketo-api/tests/unit/test_segment_tracker.py`

- [ ] **Step 1: Write the failing tests**

Create `rehketo-api/tests/unit/test_segment_tracker.py`:

```python
from __future__ import annotations

from rehketo.agent.segments import SegmentTracker


def test_empty_tracker_has_empty_answer_and_no_thinking() -> None:
    t = SegmentTracker()
    assert t.answer_text == ""
    assert t.thinking == []


def test_single_message_id_is_one_segment_with_no_thinking() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "hel")
    t.add_delta("m1", "lo")
    assert t.answer_text == "hello"
    assert t.thinking == []


def test_message_id_change_moves_prior_segment_to_thinking() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "let me check")
    t.add_delta("m2", "It is sunny.")
    assert [s.text for s in t.thinking] == ["let me check"]
    assert t.thinking[0].message_id == "m1"
    assert t.answer_text == "It is sunny."


def test_three_turns_yield_two_thinking_segments_in_order() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "first")
    t.add_delta("m2", "second")
    t.add_delta("m3", "answer")
    assert [s.text for s in t.thinking] == ["first", "second"]
    assert t.answer_text == "answer"


def test_last_delta_at_advances_within_a_segment() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "a")
    first = t.thinking_or_current()[-1].last_delta_at
    t.add_delta("m1", "b")
    assert t.thinking_or_current()[-1].last_delta_at >= first


def test_none_message_ids_group_into_one_segment() -> None:
    t = SegmentTracker()
    t.add_delta(None, "a")
    t.add_delta(None, "b")
    assert t.answer_text == "ab"
    assert t.thinking == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd rehketo-api && uv run pytest tests/unit/test_segment_tracker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rehketo.agent.segments'`.

- [ ] **Step 3: Implement the tracker**

Create `rehketo-api/rehketo/agent/segments.py`:

```python
"""Segment tracking for streamed agent runs. A tool-using run emits multiple
AI messages (narration before each tool call, the answer after); each is a
segment, keyed by the message_id on its deltas. All segments but the last are
"thinking"; the last is the answer the UI renders as the reply bubble."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Segment:
    message_id: str | None
    text: str = ""
    # Time of the segment's most recent delta. Persisted as the thinking
    # row's created_at: a turn's last token always arrives before the adapter
    # publishes the tool.call it triggered, so this timestamp sorts the
    # narration BEFORE its tool row in the transcript. (The boundary itself
    # is only detectable later — at the next segment's first delta — which
    # would sort after the tool rows.)
    last_delta_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SegmentTracker:
    def __init__(self) -> None:
        self._segments: list[Segment] = []

    def add_delta(self, message_id: str | None, delta: str) -> None:
        current = self._segments[-1] if self._segments else None
        if current is None or current.message_id != message_id:
            current = Segment(message_id=message_id)
            self._segments.append(current)
        current.text += delta
        current.last_delta_at = datetime.now(UTC)

    @property
    def thinking(self) -> list[Segment]:
        """Every segment but the last — narration that led to tool calls."""
        return self._segments[:-1]

    @property
    def answer_text(self) -> str:
        """The final segment's text; empty when the run produced none."""
        return self._segments[-1].text if self._segments else ""

    def thinking_or_current(self) -> list[Segment]:
        """All segments including the in-progress one (test/introspection)."""
        return list(self._segments)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd rehketo-api && uv run pytest tests/unit/test_segment_tracker.py -v`
Expected: all 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo-api/rehketo/agent/segments.py rehketo-api/tests/unit/test_segment_tracker.py
git commit -m "feat(api): segment tracker for thinking/answer separation"
```

---

### Task 3: Wire segments through run_agent persistence + events (api)

Replace the `assembled_text` blob with the tracker on all three terminal branches; emit one `message.complete` per persisted row on success.

**Files:**
- Modify: `rehketo-api/rehketo/agent/run.py`
- Test: `rehketo-api/tests/integration/test_run_thinking_segments.py` (create)

- [ ] **Step 1: Write the failing integration test**

Create `rehketo-api/tests/integration/test_run_thinking_segments.py`. It reuses the fake-agent harness pattern from `test_run_agent_tools.py` (same fixtures: `settings_env, db_url, db, monkeypatch`):

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, ToolMessage
from sqlalchemy import text

import rehketo.agent.run as run_mod
from rehketo.agent.run import _load_history
from rehketo.db import sessionmaker
from rehketo.db.models import Conversation, McpServer, Run, User, UserRole
from rehketo.mcp import registry
from rehketo.runs.event_bus import PostgresEventBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence


async def _seed(db) -> Any:
    u = User(id=uuid4(), display_name="Al", email=f"{uuid4()}@example.com")
    db.add(u)
    await db.commit()
    db.add(UserRole(user_id=u.id, role="User"))
    conv = Conversation(id=uuid4(), user_id=u.id)
    db.add(conv)
    await db.commit()
    run = Run(
        id=uuid4(),
        conversation_id=conv.id,
        user_id=u.id,
        status="queued",
        model="claude-sonnet-4-6",
    )
    db.add(run)
    db.add(
        McpServer(
            id=uuid4(),
            name="testsrv",
            url="https://unused.example.com/mcp",
            auth_token_ct=None,
            allowed_roles=["User"],
            enabled=True,
            auto_approve=True,
        )
    )
    await db.commit()
    return run.id, conv.id


async def test_two_turn_run_persists_thinking_and_answer_rows(
    settings_env, db_url, db, monkeypatch
) -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("echo")

    @server.tool
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo: {text}"

    monkeypatch.setattr(registry, "_client_for", lambda s: Client(server))

    class _TwoTurnAgent:
        def __init__(self, tools: Sequence[Any]) -> None:
            self._tools = tools

        async def astream(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any]:
            # Turn 1: narration, then the tool call.
            yield (AIMessageChunk(content="let me check ", id="turn-1"), {})
            yield (AIMessageChunk(content="the weather", id="turn-1"), {})
            await self._tools[0].ainvoke({"text": "boise"})
            # LangGraph also yields the ToolMessage on this stream mode; the
            # transform must drop it (the clobbering bug).
            yield (ToolMessage(content="echo: boise", tool_call_id="c1"), {})
            # Turn 2: the answer.
            yield (AIMessageChunk(content="It is sunny.", id="turn-2"), {})

    async def _fake_build_agent(
        run_id: str,
        system_prompt: str,
        tools: Sequence[Any] = (),
        interrupt_on: Any = None,
    ) -> AsyncIterator[_TwoTurnAgent]:
        yield _TwoTurnAgent(tools)

    monkeypatch.setattr(run_mod, "build_agent", _fake_build_agent)

    run_id, conv_id = await _seed(db)
    bus = PostgresEventBus()
    await run_mod.run_agent(run_id, bus)

    # --- Persistence: one thinking row + one answer row, correctly ordered.
    async with sessionmaker()() as s:
        msg_rows = (
            await s.execute(
                text(
                    "SELECT content, created_at FROM messages "
                    "WHERE run_id = :rid AND role = 'assistant' "
                    "ORDER BY created_at"
                ),
                {"rid": str(run_id)},
            )
        ).all()
        tool_call_at = (
            await s.execute(
                text(
                    "SELECT created_at FROM run_events WHERE run_id = :rid "
                    "AND payload->>'type' = 'tool.call'"
                ),
                {"rid": str(run_id)},
            )
        ).scalar_one()

    assert len(msg_rows) == 2
    thinking, answer = msg_rows
    assert thinking.content == {"text": "let me check the weather", "channel": "thinking"}
    assert answer.content == {"text": "It is sunny."}
    # Narration interleaves BEFORE the tool row it triggered.
    assert thinking.created_at < tool_call_at

    # --- No leak: the tool output appears in no message row.
    assert all("echo:" not in str(r.content) for r in msg_rows)

    # --- Events: a message.complete per row, answer last; no delta carries
    # the tool output.
    async with sessionmaker()() as s:
        events = (
            await s.execute(
                text(
                    "SELECT payload FROM run_events WHERE run_id = :rid "
                    "ORDER BY sequence"
                ),
                {"rid": str(run_id)},
            )
        ).all()
    payloads = [r.payload for r in events]
    completes = [p for p in payloads if p["type"] == "message.complete"]
    assert len(completes) == 2
    assert completes[0]["message"]["content"]["channel"] == "thinking"
    assert "channel" not in completes[1]["message"]["content"]
    deltas = [p for p in payloads if p["type"] == "message.delta"]
    assert all("echo:" not in p["delta"] for p in deltas)

    # --- History: only the answer feeds back to the model.
    async with sessionmaker()() as s:
        history = await _load_history(s, conv_id)
    assert [m.content for m in history] == ["It is sunny."]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd rehketo-api && uv run pytest tests/integration/test_run_thinking_segments.py -v`
Expected: FAIL — `len(msg_rows) == 2` is 1 (single concatenated row today).

- [ ] **Step 3: Replace assembled_text with the tracker in run.py**

In `rehketo-api/rehketo/agent/run.py`:

3a. Add the import:

```python
from rehketo.agent.segments import SegmentTracker
```

3b. Add a module-level helper after `_load_history`:

```python
def _assistant_rows(
    segments: SegmentTracker, conversation_id: UUID, run_id: UUID
) -> list[Message]:
    """One Message row per AI turn. Thinking rows carry channel='thinking'
    and their last-delta timestamp so they interleave correctly with the
    adapter-persisted tool rows; the final turn is the answer — plain {text}
    content, created_at left to the DB default. An empty run still persists
    the single empty answer row (it marks that an attempt happened)."""
    rows = [
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="assistant",
            content={"text": seg.text, "channel": "thinking"},
            run_id=run_id,
            created_at=seg.last_delta_at,
        )
        for seg in segments.thinking
    ]
    rows.append(
        Message(
            id=uuid4(),
            conversation_id=conversation_id,
            role="assistant",
            content={"text": segments.answer_text},
            run_id=run_id,
        )
    )
    return rows
```

3c. Replace `assembled_text = ""` with `segments = SegmentTracker()`, and in the stream loop replace

```python
                            for event in transform_chunk(chunk):  # type: ignore[arg-type]
                                await bus.publish(str(run_id), event)
                                if event["type"] == "message.delta":
                                    assembled_text += str(event["delta"])
```

with

```python
                            for event in transform_chunk(chunk):  # type: ignore[arg-type]
                                await bus.publish(str(run_id), event)
                                if event["type"] == "message.delta":
                                    segments.add_delta(
                                        event.get("message_id"), str(event["delta"])
                                    )
```

3d. Success branch — replace the block from `assistant_id = uuid4()` through the `message_payload` assignment (run.py:154-198) with:

```python
            # Persist one assistant row per AI turn and finalize the run.
            rows = _assistant_rows(segments, conversation_id, run_id)
            async with sessionmaker()() as db:
                for row in rows:
                    db.add(row)
                await db.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(
                        status="succeeded",
                        finished_at=datetime.now(UTC),
                    )
                )
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(updated_at=datetime.now(UTC))
                )
                await db.commit()
                # Refresh each row so the wire shape matches the MessageOut
                # that GET /conversations/{id} returns (DB-assigned
                # created_at on the answer row). The UI replaces its
                # streaming state with these server-authoritative objects.
                message_payloads: list[dict[str, object]] = []
                for row in rows:
                    await db.refresh(row)
                    message_payloads.append(
                        {
                            "id": str(row.id),
                            "conversation_id": str(row.conversation_id),
                            "role": row.role,
                            "content": row.content,
                            "run_id": str(row.run_id) if row.run_id else None,
                            "created_at": row.created_at.isoformat()
                            if row.created_at
                            else None,
                            "run_status": "succeeded",
                            "run_error": None,
                        }
                    )

            # Thinking rows first, answer last — the UI ends its streaming
            # bubble on the answer's complete.
            for payload in message_payloads:
                await bus.publish(
                    str(run_id),
                    {
                        "type": "message.complete",
                        "message": payload,
                    },
                )
```

3e. Cancel branch — in `_finalize_cancel`, replace the single `db.add(Message(...))` call (and its comment) with:

```python
                    # Persist the segments under the same rule as success —
                    # completed turns as thinking, the partial tail as the
                    # answer. Reload shows a 'cancelled' badge via the
                    # run_status join on MessageOut.
                    for row in _assistant_rows(segments, conversation_id, run_id):
                        db.add(row)
```

3f. Failure branch — same replacement for its `db.add(Message(...))`:

```python
                # Persist whatever partial segments the stream produced —
                # same thinking/answer rule as success. GET /conversations/{id}
                # joins Run.status/Run.error so the UI renders the answer row
                # with a 'failed' badge on reload. Empty text is fine — it
                # still marks that an attempt happened.
                for row in _assistant_rows(segments, conversation_id, run_id):
                    db.add(row)
```

3g. `_load_history` — add the skip at the top of the loop body:

```python
    for m in msgs:
        if isinstance(m.content, dict) and m.content.get("channel") == "thinking":
            # Narration is not model context — only answers feed back,
            # matching how Anthropic drops thinking between turns.
            continue
```

- [ ] **Step 4: Run the new test and the existing run-path tests**

Run: `cd rehketo-api && uv run pytest tests/integration/test_run_thinking_segments.py tests/integration/test_run_agent_tools.py tests/integration/test_run_agent_end_to_end.py tests/integration/test_run_outcome_persistence.py tests/integration/test_run_cancel.py -v`
Expected: all PASS. (Existing tests stream a single `message_id`, so they produce one answer row — unchanged behavior.)

- [ ] **Step 5: Run the full api suite**

Run: `cd rehketo-api && uv run pytest`
Expected: PASS. If an approval/cancel test asserts on the old single-message shape, fix the assertion to the new two-row rule — do not weaken the assertion, update it to assert the channel split explicitly.

- [ ] **Step 6: Commit**

```bash
git add rehketo-api/rehketo/agent/run.py rehketo-api/tests/integration/test_run_thinking_segments.py
git commit -m "feat(api): persist agent turns as thinking/answer message rows"
```

---

### Task 4: API static checks + contract guard

**Files:** none new — validation only.

- [ ] **Step 1: Run the api check block**

```bash
cd rehketo-api
uv run ruff format --check
uv run ruff check
uv run mypy rehketo
uv run bandit -r rehketo
uv run lint-imports
uv run python ../tools/check_contract.py
```

Expected: all clean. `check_contract.py` passes without rebaseline — `content` is `dict[str, object]` on the wire, so the optional `channel` key changes no schema.

- [ ] **Step 2: Run the repo guards**

```bash
cd /Users/adama/workspace/rehketo
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

Expected: both pass. Fix anything flagged before proceeding (no commit here unless fixes were needed; if so: `git commit -m "chore(api): satisfy guards after segment persistence"`).

---

### Task 5: UI contract type + pure transcript grouping

**Files:**
- Modify: `rehketo-ui/src/lib/types.ts:74-76`
- Create: `rehketo-ui/src/lib/transcript.ts`
- Test: `rehketo-ui/src/lib/transcript.spec.ts` (node project — no DOM)

- [ ] **Step 1: Write the failing grouping tests**

Create `rehketo-ui/src/lib/transcript.spec.ts`:

```typescript
import { describe, expect, it } from 'vitest';

import { groupTranscript } from './transcript';
import type { MessageItem, ToolCallItem, TranscriptItem } from './types';

function msg(overrides: Partial<MessageItem>): MessageItem {
	return {
		kind: 'message',
		id: 'm-1',
		conversation_id: 'c-1',
		role: 'assistant',
		content: { text: 'hi' },
		run_id: 'run-1',
		created_at: '2026-06-12T00:00:00Z',
		run_status: null,
		run_error: null,
		...overrides
	};
}

function tool(overrides: Partial<ToolCallItem> = {}): ToolCallItem {
	return {
		kind: 'tool',
		run_id: 'run-1',
		call_id: 'call-1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		result: 'echo: hi',
		is_error: false,
		created_at: '2026-06-12T00:00:01Z',
		...overrides
	};
}

describe('groupTranscript', () => {
	it('keeps user and answer messages as standalone bubbles', () => {
		const items: TranscriptItem[] = [
			msg({ id: 'u-1', role: 'user', run_id: null }),
			msg({ id: 'a-1', content: { text: 'answer' } })
		];
		const groups = groupTranscript(items);
		expect(groups.map((g) => g.kind)).toEqual(['bubble', 'bubble']);
	});

	it('collapses a run’s thinking, tool, and approval items into one working group', () => {
		const items: TranscriptItem[] = [
			msg({ id: 'u-1', role: 'user', run_id: null }),
			msg({ id: 't-1', content: { text: 'let me check', channel: 'thinking' } }),
			tool(),
			{
				kind: 'approval',
				run_id: 'run-1',
				approval_id: 'ap-1',
				tool: 'testsrv__echo',
				arguments: {},
				decision: 'approve',
				created_at: '2026-06-12T00:00:02Z'
			},
			msg({ id: 'a-1', content: { text: 'It is sunny.' } })
		];
		const groups = groupTranscript(items);
		expect(groups.map((g) => g.kind)).toEqual(['bubble', 'working', 'bubble']);
		const working = groups[1];
		if (working?.kind !== 'working') throw new Error('expected working group');
		expect(working.runId).toBe('run-1');
		expect(working.entries.map((e) => e.kind)).toEqual(['text', 'tool', 'approval']);
	});

	it('keeps working groups of different runs separate', () => {
		const items: TranscriptItem[] = [tool({ run_id: 'run-1' }), tool({ run_id: 'run-2' })];
		const groups = groupTranscript(items);
		expect(groups.map((g) => (g.kind === 'working' ? g.runId : ''))).toEqual([
			'run-1',
			'run-2'
		]);
	});

	it('tool-only runs still get a working group', () => {
		const groups = groupTranscript([tool(), msg({ id: 'a-1' })]);
		expect(groups.map((g) => g.kind)).toEqual(['working', 'bubble']);
	});
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/transcript.spec.ts`
Expected: FAIL — `./transcript` does not exist.

- [ ] **Step 3: Add the channel field and implement groupTranscript**

In `rehketo-ui/src/lib/types.ts`, change `MessageContent` to:

```typescript
export type MessageContent = {
	text: string;
	// Present on intermediate "thinking" turns of a tool-using run; absent
	// on answers and user messages. Matches the api's per-turn persistence
	// (rehketo/agent/run.py _assistant_rows).
	channel?: 'thinking';
};
```

Create `rehketo-ui/src/lib/transcript.ts`:

```typescript
// Pure grouping of transcript items into render groups: a run's thinking
// messages, tool calls, and approval cards collapse into one Working block;
// answers and user messages stay standalone bubbles. Pure function so the
// node test project covers it without DOM.

import type { ApprovalItem, MessageItem, ToolCallItem, TranscriptItem } from './types';

export type WorkingEntry =
	| { kind: 'text'; text: string }
	| { kind: 'tool'; item: ToolCallItem }
	| { kind: 'approval'; item: ApprovalItem };

export type RenderGroup =
	| { kind: 'bubble'; item: MessageItem }
	| { kind: 'working'; runId: string; entries: WorkingEntry[] };

export function groupTranscript(items: TranscriptItem[]): RenderGroup[] {
	const groups: RenderGroup[] = [];
	const workingFor = (runId: string): Extract<RenderGroup, { kind: 'working' }> => {
		const last = groups[groups.length - 1];
		if (last && last.kind === 'working' && last.runId === runId) return last;
		const next = { kind: 'working' as const, runId, entries: [] as WorkingEntry[] };
		groups.push(next);
		return next;
	};
	for (const item of items) {
		if (item.kind === 'tool') {
			workingFor(item.run_id).entries.push({ kind: 'tool', item });
		} else if (item.kind === 'approval') {
			workingFor(item.run_id).entries.push({ kind: 'approval', item });
		} else if (item.content.channel === 'thinking' && item.run_id !== null) {
			workingFor(item.run_id).entries.push({ kind: 'text', text: item.content.text });
		} else {
			groups.push({ kind: 'bubble', item });
		}
	}
	return groups;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/transcript.spec.ts`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add rehketo-ui/src/lib/types.ts rehketo-ui/src/lib/transcript.ts rehketo-ui/src/lib/transcript.spec.ts
git commit -m "feat(ui): thinking channel type and transcript grouping"
```

---

### Task 6: WorkingBlock component + MessageList grouping

**Files:**
- Create: `rehketo-ui/src/lib/components/WorkingBlock.svelte`
- Modify: `rehketo-ui/src/lib/components/MessageList.svelte`
- Test: `rehketo-ui/src/lib/components/MessageList.dom.spec.ts` (extend)

- [ ] **Step 1: Extend the MessageList dom spec with grouping tests**

Append to the `describe('MessageList', ...)` block in `rehketo-ui/src/lib/components/MessageList.dom.spec.ts` (add `MessageItem`, `ToolCallItem` to the type import from `$lib/types`):

```typescript
	function thinkingItem(overrides: Partial<MessageItem> = {}): MessageItem {
		return {
			kind: 'message',
			id: 'think-1',
			conversation_id: 'c-1',
			role: 'assistant',
			content: { text: 'let me check', channel: 'thinking' },
			run_id: 'run-1',
			created_at: '2026-06-12T00:00:00Z',
			run_status: null,
			run_error: null,
			...overrides
		};
	}

	function toolItem(overrides: Partial<ToolCallItem> = {}): ToolCallItem {
		return {
			kind: 'tool',
			run_id: 'run-1',
			call_id: 'call-1',
			tool: 'testsrv__echo',
			arguments: { text: 'hi' },
			result: 'echo: hi',
			is_error: false,
			created_at: '2026-06-12T00:00:01Z',
			...overrides
		};
	}

	it('groups thinking text and tool chips into one working block', () => {
		const app = mount(MessageList, {
			target: document.body,
			props: { items: [thinkingItem(), toolItem()], liveRunId: null }
		});
		const blocks = document.querySelectorAll('[data-working]');
		expect(blocks.length).toBe(1);
		expect(blocks[0]?.textContent).toContain('let me check');
		expect(blocks[0]?.querySelector('[data-status]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('keeps the answer message outside the working block', () => {
		const answer = thinkingItem({ id: 'ans-1', content: { text: 'It is sunny.' } });
		const app = mount(MessageList, {
			target: document.body,
			props: { items: [thinkingItem(), toolItem(), answer], liveRunId: null }
		});
		expect(document.querySelector('[data-working]')?.textContent).not.toContain('It is sunny.');
		expect(document.body.textContent).toContain('It is sunny.');
		unmount(app);
		document.body.innerHTML = '';
	});

	it('approval buttons stay actionable inside a live working block', () => {
		const app = mount(MessageList, {
			target: document.body,
			props: { items: [thinkingItem(), approvalItem()], liveRunId: 'run-1', canDecide: true }
		});
		const block = document.querySelector('[data-working]');
		expect(block).not.toBeNull();
		expect(block?.querySelector('[data-action="approve"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
```

- [ ] **Step 2: Run to verify the new tests fail**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/MessageList.dom.spec.ts`
Expected: the 3 new tests FAIL (no `[data-working]` element); the 2 existing approval tests still PASS.

- [ ] **Step 3: Create WorkingBlock.svelte**

Create `rehketo-ui/src/lib/components/WorkingBlock.svelte`:

```svelte
<script lang="ts">
	import ApprovalCard from './ApprovalCard.svelte';
	import ToolChip from './ToolChip.svelte';
	import type { WorkingEntry } from '$lib/transcript';
	import type { ApprovalItem } from '$lib/types';

	let {
		entries,
		live = false,
		canDecide = false,
		onDecide
	}: {
		entries: WorkingEntry[];
		live?: boolean;
		canDecide?: boolean;
		onDecide?: (item: ApprovalItem, decision: 'approve' | 'deny') => void;
	} = $props();

	// A pending approval keeps the block open even after the run pauses —
	// the decision buttons live inside it.
	let pendingApproval = $derived(
		entries.some((e) => e.kind === 'approval' && e.item.decision === null)
	);
	let label = $derived(`Working… (${entries.length} ${entries.length === 1 ? 'step' : 'steps'})`);
</script>

<details
	open={live || pendingApproval}
	data-working
	class="rounded-md border border-border bg-surface/40 text-xs text-muted"
>
	<summary class="cursor-pointer px-3 py-1.5">{label}</summary>
	<div class="space-y-2 border-t border-border px-3 py-2">
		{#each entries as entry, i (i)}
			{#if entry.kind === 'text'}
				<p class="whitespace-pre-wrap">{entry.text}</p>
			{:else if entry.kind === 'tool'}
				<ToolChip item={entry.item} {live} />
			{:else}
				<ApprovalCard
					item={entry.item}
					{canDecide}
					onDecide={(decision) => onDecide?.(entry.item, decision)}
				/>
			{/if}
		{/each}
	</div>
</details>
```

Note: check `ApprovalCard.svelte`'s actual props signature before wiring `onDecide` — MessageList currently passes `onDecide={(decision) => onDecide?.(item, decision)}`, so the card takes `(decision)` only; mirror exactly what MessageList does today.

- [ ] **Step 4: Rewrite MessageList rendering over groups**

In `rehketo-ui/src/lib/components/MessageList.svelte`: add imports

```typescript
	import WorkingBlock from './WorkingBlock.svelte';
	import { groupTranscript } from '$lib/transcript';
```

add the derived groups after the props:

```typescript
	let groups = $derived(groupTranscript(items));
```

and replace the `{#each items ...}` block with:

```svelte
		{#each groups as group (group.kind === 'bubble' ? group.item.id : `working:${group.runId}`)}
			<li>
				{#if group.kind === 'bubble'}
					<MessageBubble message={group.item} />
				{:else}
					<WorkingBlock
						entries={group.entries}
						live={group.runId === liveRunId}
						canDecide={canDecide && group.runId === liveRunId}
						{onDecide}
					/>
				{/if}
			</li>
		{/each}
```

Remove the now-unused `ToolChip`, `ApprovalCard`, and `ApprovalItem` imports from MessageList **only if** nothing else in the file references them (the `onDecide` prop type still needs `ApprovalItem`). The streaming-bubble tail (`showStreamingBubble` block) stays exactly as is.

- [ ] **Step 5: Run the dom specs**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/MessageList.dom.spec.ts`
Expected: all 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add rehketo-ui/src/lib/components/WorkingBlock.svelte rehketo-ui/src/lib/components/MessageList.svelte rehketo-ui/src/lib/components/MessageList.dom.spec.ts
git commit -m "feat(ui): collapsible working block for thinking and tool activity"
```

---

### Task 7: ChatView segment folding

**Files:**
- Modify: `rehketo-ui/src/lib/components/ChatView.svelte`
- Test: `rehketo-ui/src/lib/components/ChatView.dom.spec.ts` (extend)

- [ ] **Step 1: Write the failing folding tests**

Append a new describe block to `rehketo-ui/src/lib/components/ChatView.dom.spec.ts` (reuses the existing `conversation()` helper and mocked `subscribeRun`):

```typescript
describe('ChatView segment folding', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	function handlersFor(runId: string): RunStreamHandlers {
		mount(ChatView, {
			target: document.body,
			props: { conversation: conversation(runId) }
		});
		return vi.mocked(subscribeRun).mock.calls[0][1] as RunStreamHandlers;
	}

	it('folds the current segment into a thinking item when a tool.call arrives', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000c';
		const handlers = handlersFor(runId);

		flushSync(() => {
			handlers.onDelta?.('let me check', {
				type: 'message.delta',
				delta: 'let me check',
				message_id: 'turn-1',
				sequence: 1,
				run_id: runId
			});
		});
		flushSync(() => {
			handlers.onToolCall?.({
				type: 'tool.call',
				run_id: runId,
				call_id: 'call_0',
				tool: 'testsrv__echo',
				arguments: {},
				sequence: 2
			});
		});

		// The narration moved INTO the working block, above the tool chip.
		const block = document.querySelector('[data-working]');
		expect(block?.textContent).toContain('let me check');
		expect(block?.querySelector('[data-status="running"]')).not.toBeNull();
		document.body.innerHTML = '';
	});

	it('a new message_id after the tool result streams as the answer tail', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000d';
		const handlers = handlersFor(runId);

		flushSync(() => {
			handlers.onDelta?.('narration', {
				type: 'message.delta',
				delta: 'narration',
				message_id: 'turn-1',
				sequence: 1,
				run_id: runId
			});
		});
		flushSync(() => {
			handlers.onDelta?.('It is sunny.', {
				type: 'message.delta',
				delta: 'It is sunny.',
				message_id: 'turn-2',
				sequence: 2,
				run_id: runId
			});
		});

		const block = document.querySelector('[data-working]');
		expect(block?.textContent).toContain('narration');
		expect(block?.textContent).not.toContain('It is sunny.');
		expect(document.body.textContent).toContain('It is sunny.');
		document.body.innerHTML = '';
	});

	it('replaces local thinking items with persisted rows on message.complete', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000e';
		const handlers = handlersFor(runId);

		flushSync(() => {
			handlers.onDelta?.('narration', {
				type: 'message.delta',
				delta: 'narration',
				message_id: 'turn-1',
				sequence: 1,
				run_id: runId
			});
		});
		flushSync(() => {
			handlers.onDelta?.('answer', {
				type: 'message.delta',
				delta: 'answer',
				message_id: 'turn-2',
				sequence: 2,
				run_id: runId
			});
		});
		flushSync(() => {
			handlers.onMessageComplete?.({
				id: 'persisted-think-1',
				conversation_id: 'c0000000-0000-0000-0000-000000000001',
				role: 'assistant',
				content: { text: 'narration', channel: 'thinking' },
				run_id: runId,
				created_at: '2026-06-12T00:00:00Z',
				run_status: 'succeeded',
				run_error: null
			});
		});
		flushSync(() => {
			handlers.onMessageComplete?.({
				id: 'persisted-ans-1',
				conversation_id: 'c0000000-0000-0000-0000-000000000001',
				role: 'assistant',
				content: { text: 'answer' },
				run_id: runId,
				created_at: '2026-06-12T00:00:01Z',
				run_status: 'succeeded',
				run_error: null
			});
		});

		// Exactly one copy of the narration (persisted row, not the local fold).
		const matches = document.body.textContent?.split('narration').length;
		expect(matches).toBe(2); // one occurrence → split yields 2 parts
		expect(document.body.textContent).toContain('answer');
		document.body.innerHTML = '';
	});
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/ChatView.dom.spec.ts`
Expected: the 3 new tests FAIL (no working block / duplicated narration); existing tests PASS.

- [ ] **Step 3: Implement tail folding in ChatView**

In `rehketo-ui/src/lib/components/ChatView.svelte`:

3a. Add state next to `streamingText`:

```typescript
	let streamingMessageId = $state<string | null>(null);
	// Monotonic suffix for local thinking ids — replaced by persisted rows
	// when message.complete arrives.
	let localThinkingSeq = 0;
```

3b. Add the fold helper after `resetStreaming` (and add `streamingMessageId = null;` inside `resetStreaming`):

```typescript
	// The current streaming segment is proven to be narration (not the
	// answer) the moment the model calls a tool, asks for approval, or
	// starts a new message. Fold it into a local thinking item so it
	// renders inside the working block, above the activity it led to.
	function foldStreamingTail(): void {
		const text = streamingText;
		streamingMessageId = null;
		if (text === null || text.length === 0 || activeRunId === null) return;
		const folded: MessageOut = {
			id: `local-thinking-${activeRunId}-${localThinkingSeq++}`,
			conversation_id: conversation.id,
			role: 'assistant',
			content: { text, channel: 'thinking' },
			run_id: activeRunId,
			created_at: new Date(Date.now()).toISOString(),
			run_status: null,
			run_error: null
		};
		items = [...items, { ...folded, kind: 'message' as const }];
		streamingText = '';
	}
```

3c. Replace the `onDelta` handler:

```typescript
			onDelta: (delta, event) => {
				if (streamingMessageId !== null && streamingMessageId !== event.message_id) {
					foldStreamingTail();
				}
				streamingMessageId = event.message_id;
				streamingText = (streamingText ?? '') + delta;
			},
```

3d. Replace the `onMessageComplete` handler:

```typescript
			onMessageComplete: (message) => {
				// Persisted rows replace the local thinking items synthesized
				// during streaming — same text, server-authoritative ids.
				items = items.filter(
					(i) => !(i.kind === 'message' && i.id.startsWith(`local-thinking-${message.run_id}`))
				);
				// Replay can deliver a message.complete the conversation GET
				// already included — dedupe by id rather than trust ordering.
				if (!items.some((i) => i.kind === 'message' && i.id === message.id)) {
					items = [...items, { ...message, kind: 'message' as const }];
				}
				// Thinking rows arrive first; only the answer's complete (no
				// channel marker) ends the streaming bubble.
				if (message.content.channel !== 'thinking') {
					streamingText = null;
					streamingMessageId = null;
				}
			},
```

3e. In `onToolCall` and `onApprovalRequired`, add `foldStreamingTail();` as the **first** line of each handler (before the dedupe check) — the chip/card is then appended after the folded narration, preserving order.

- [ ] **Step 4: Run the dom specs**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/ChatView.dom.spec.ts`
Expected: all PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add rehketo-ui/src/lib/components/ChatView.svelte rehketo-ui/src/lib/components/ChatView.dom.spec.ts
git commit -m "feat(ui): fold streaming narration into thinking items"
```

---

### Task 8: Full validation, spec amendment, final commit

**Files:**
- Modify: `docs/superpowers/specs/2026-06-12-thinking-separation-design.md` (mechanism notes)

- [ ] **Step 1: Full UI check block**

```bash
cd rehketo-ui
pnpm run lint
pnpm run check
pnpm run test:unit -- --run
```

Expected: all clean. Quote real output.

- [ ] **Step 2: Full API check block** (re-run; Task 7 touched no api code, this is the final gate)

```bash
cd rehketo-api
uv run ruff format --check && uv run ruff check && uv run mypy rehketo && uv run bandit -r rehketo && uv run lint-imports && uv run pytest && uv run python ../tools/check_contract.py
```

Expected: all clean.

- [ ] **Step 3: Repo guards**

```bash
cd /Users/adama/workspace/rehketo
uv run --project rehketo-api python tools/agent_guards.py check
uv run --project rehketo-api python tools/sync_agent_rules.py --check
```

Expected: both pass.

- [ ] **Step 4: Amend the spec with the two mechanism refinements**

In `docs/superpowers/specs/2026-06-12-thinking-separation-design.md`:

Replace the sentence in the run.py section: "a delta with a new `message_id` closes the current segment, stamping `closed_at = now()` so its `created_at` interleaves correctly with the adapter-persisted tool rows." with:

"a delta with a new `message_id` closes the current segment. The segment's persisted `created_at` is its **last-delta time** (updated on every delta), because the boundary is only detectable at the next segment's first delta — after the tool rows were written. The last token of a turn always precedes its `tool.call` publish, so last-delta time interleaves correctly with the adapter-persisted tool rows."

Replace the ChatView paragraph: "`streamingText: string | null` becomes a segment list `{ messageId: string; text: string }[]` driven by `onDelta`'s `event.message_id`. The last segment renders as the normal streaming bubble. Earlier segments — plus the live run's tool chips and approval cards — render inside the Working block." with:

"`streamingText` keeps only the current segment (paired with `streamingMessageId`). A finished segment folds into a local thinking transcript item the moment a `tool.call`, `tool.approval_required`, or new-`message_id` delta proves the turn ended — so it renders inside the Working block above the activity it led to, and persisted rows replace the local items on `message.complete`. The current segment renders as the normal streaming bubble."

- [ ] **Step 5: Commit the spec amendment**

```bash
git add docs/superpowers/specs/2026-06-12-thinking-separation-design.md
git commit -m "docs(specs): record segment-timestamp and tail-fold refinements"
```

- [ ] **Step 6: Manual smoke (optional but recommended)**

`just db`, `just api`, `just ui` in three terminals; open a conversation against an MCP-enabled server and confirm: narration + chips collapse into "Working…", answer renders alone, reload reproduces the same layout, and no raw tool output appears in any bubble.
