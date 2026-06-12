# Thinking/answer separation in streamed runs

**Date:** 2026-06-12
**Status:** Approved design. Fixes the M3/M3.5 streaming regression where MCP
tool output and multi-turn agent text clobber into one assistant bubble.

## What and why

Two distinct problems share one symptom (a garbled assistant bubble in any
chat that uses MCP tools):

1. **Tool results leak into the delta stream.** LangGraph's
   `stream_mode="messages"` yields every message a node produces — including
   `ToolMessage`s carrying raw MCP tool output. `transform_chunk`
   (`rehketo/agent/events.py`) never checks message type, so tool result text
   streams out as `message.delta` and lands in the assistant bubble,
   duplicating what the ToolChip already renders from the adapter's
   `tool.result` event. This is a bug, full stop.
2. **Multi-turn agent text has no boundaries.** A tool-using run produces
   multiple AI messages (narration before each tool call, the answer after).
   The UI concatenates every delta into one `streamingText`, and `run_agent`
   persists the same concatenation as a single message. Narration and answer
   fuse with no separation.

The fix: filter the stream to AI chunks only, and treat each AI message as a
segment. Intermediate segments are "thinking"; the last segment is the
answer. The UI renders thinking segments plus the run's tool chips and
approval cards inside a dim, collapsible **Working** block above the normal
answer bubble.

## Scope decisions

- **"Thinking" is structural, not token-typed.** `build_chat_model` does not
  request extended thinking; the model emits none. Thinking here means "AI
  turns before the run's final one" — identified by the `message_id` changing
  between deltas. Enabling real extended thinking later lands on the same
  `channel` marker; that is a seam, not a feature built now (charter rule 3).
- **One `Message` row per AI turn** (approach A). Intermediate turns persist
  with content `{"text": ..., "channel": "thinking"}`; the answer persists as
  plain `{"text": ...}` — no `channel` key, so existing rows and consumers
  stay valid. Rides the existing `created_at`-ordered transcript
  interleaving; no schema migration (content is already JSON).
- **History feeds answers only.** `_load_history` skips
  `channel == "thinking"` messages, matching how Anthropic drops thinking
  from subsequent turns.
- **Same segment rule on every terminal branch.** Success, failure, and
  cancel all persist completed segments as thinking and the last (possibly
  partial) segment as the answer. Failed/cancelled badges attach to the
  answer row exactly as today.
- **No new SSE event types.** `message.delta` already carries `message_id`;
  the UI starts honoring it as the segment boundary. Replay-from-zero
  resume works unchanged.
- **Tool-only runs get a Working block too.** Grouping is uniform: a run's
  thinking messages, tool items, and approval items form the block whether or
  not narration text exists. One rule, no special case.
- **Out of scope:** enabling extended thinking on the model, streaming
  per-segment persistence (segments persist at finalize, with timestamps
  captured at boundary time).

## API changes

### `rehketo/agent/events.py`

`transform_chunk` emits `message.delta` only when the chunk's message is an
`AIMessage`/`AIMessageChunk` (real `isinstance` check against
`langchain_core.messages`). `ToolMessage`s and anything else yield nothing —
the MCP adapter's `tool.call`/`tool.result` events are the sole carriers of
tool traffic. Unit tests switch from duck-typed fakes to real
`langchain_core` message classes.

### `rehketo/agent/run.py`

`assembled_text` (a string) becomes a segment tracker: an ordered list of
`(message_id, text, last_delta_at)`. Each delta appends to the current
segment; a delta with a new `message_id` closes the current segment. The
segment's persisted `created_at` is its **last-delta time** (updated on
every delta), because the boundary is only detectable at the next segment's
first delta — after the tool rows were written. The last token of a turn
always precedes its `tool.call` publish, so last-delta time interleaves
correctly with the adapter-persisted tool rows. Timestamps are read back from
the delta events' `run_events.created_at` (DB clock) rather than the app
clock, so the transcript sort never compares two clock sources.

At finalize (all three terminal branches):

- every segment but the last → assistant `Message` with
  `{"text": ..., "channel": "thinking"}`, `created_at` = its `closed_at`;
- the last segment → answer with plain `{"text": ...}`, `created_at` = now;
- empty runs persist the single empty answer row, as today.

On success, `message.complete` is published once per persisted row, in
order, answer last — the live transcript converges to exactly what a
reload's GET returns, and the UI's existing dedupe-by-id absorbs replay.

### `rehketo/agent/run.py` — `_load_history`

Skips messages whose content dict has `channel == "thinking"`.

## Contract

`MessageContent` (`rehketo-ui/src/lib/types.ts`) gains optional
`channel?: 'thinking'`. `tools/check_contract.py` must pass. No OpenAPI
shape change beyond the optional content key.

## UI changes

### Live streaming (`ChatView.svelte`)

`streamingText` keeps only the current segment (paired with
`streamingMessageId`). A finished segment folds into a local thinking
transcript item the moment a `tool.call`, `tool.approval_required`, or
new-`message_id` delta proves the turn ended — so it renders inside the
Working block above the activity it led to, and persisted rows replace the
local items on `message.complete`. The current segment renders as the
normal streaming bubble. Terminal fold-in for failed/cancelled runs
follows the same last-segment-is-answer rule. Streaming state clears when
the answer's `message.complete` arrives (it is published last), not on the
thinking rows' completes — the dedupe-by-id append handles those.

### Working block (new `WorkingBlock.svelte`)

Dim, collapsible container labeled "Working… (n steps)". Auto-expanded while
the run is live or an approval is pending; collapsed once the run is
terminal. Children are thinking text segments, ToolChips, and ApprovalCards
in event order; approval cards remain fully interactive inside it.

### Transcript rendering (`MessageList.svelte`)

Items are grouped before rendering: a run's consecutive
thinking-channel messages, tool items, and approval items collapse into one
WorkingBlock; the answer message renders as a normal `MessageBubble`.
Grouping keys on `run_id` and transcript order — no timestamps parsed on the
client.

## Testing

- **Unit (api):** `transform_chunk` drops `ToolMessage` chunks and passes
  `AIMessageChunk` text through (real message classes); segment tracker
  closes on `message_id` change and stamps boundary times.
- **Integration (api):** fake-MCP run persists thinking row(s) + answer row
  with interleaving timestamps; tool result text appears in no message
  content; `_load_history` excludes thinking rows; failure and cancel
  branches persist segments under the same rule.
- **UI (dom specs):** ChatView accumulates segments by `message_id`;
  MessageList groups thinking/tool/approval items into a WorkingBlock and
  keeps the answer bubble out of it; approval buttons work inside the block.
