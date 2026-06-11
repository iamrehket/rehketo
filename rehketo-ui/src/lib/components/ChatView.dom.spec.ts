// Resume-on-open: ChatView must reattach to an in-flight run named by the
// conversation GET (active_run_id) — and must not open a stream when there
// is none. Mounted with Svelte's own mount() in the jsdom project; $lib/sse
// is mocked so no EventSource is involved.

import { mount, unmount } from 'svelte';
import { flushSync } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatView from './ChatView.svelte';
import { subscribeRun } from '$lib/sse';
import type { RunStreamHandlers } from '$lib/sse';
import type { ConversationDetail, ToolCallItem } from '$lib/types';

vi.mock('$lib/sse', () => ({
	subscribeRun: vi.fn(() => ({ state: 'idle', unsubscribe: vi.fn() }))
}));

function conversation(
	activeRunId: string | null,
	extraItems: ConversationDetail['items'] = []
): ConversationDetail {
	return {
		id: 'c0000000-0000-0000-0000-000000000001',
		title: null,
		created_at: '2026-06-10T00:00:00Z',
		updated_at: '2026-06-10T00:00:00Z',
		items: extraItems,
		active_run_id: activeRunId
	};
}

function pendingToolItem(runId: string): ToolCallItem {
	return {
		kind: 'tool',
		run_id: runId,
		call_id: 'call-1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		result: null,
		is_error: null,
		created_at: '2026-06-10T00:00:00Z'
	};
}

describe('ChatView resume-on-open', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('subscribes to active_run_id when the conversation has one', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000a';
		const app = mount(ChatView, {
			target: document.body,
			props: { conversation: conversation(runId) }
		});
		expect(subscribeRun).toHaveBeenCalledTimes(1);
		expect(vi.mocked(subscribeRun).mock.calls[0][0]).toBe(runId);
		unmount(app);
	});

	it('does not subscribe when active_run_id is null', () => {
		const app = mount(ChatView, {
			target: document.body,
			props: { conversation: conversation(null) }
		});
		expect(subscribeRun).not.toHaveBeenCalled();
		unmount(app);
	});

	it('renders a running tool chip for a pending item from the active run', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000a';
		const app = mount(ChatView, {
			target: document.body,
			props: { conversation: conversation(runId, [pendingToolItem(runId)]) }
		});
		expect(document.querySelector('[data-status="running"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});

describe('ChatView handler routing', () => {
	beforeEach(() => {
		vi.clearAllMocks();
	});

	it('routes onToolCall/onToolResult to correct chips, dedupes replays', () => {
		const runId = 'a0000000-0000-0000-0000-00000000000b';
		const app = mount(ChatView, {
			target: document.body,
			props: { conversation: conversation(runId) }
		});

		// Capture the handlers object passed to the subscribeRun mock.
		const handlers = vi.mocked(subscribeRun).mock.calls[0][1] as RunStreamHandlers;

		// Fire onToolCall for two distinct call_ids on the same run.
		flushSync(() => {
			handlers.onToolCall?.({
				type: 'tool.call',
				run_id: runId,
				call_id: 'call_0',
				tool: 'testsrv__echo',
				arguments: { text: 'a' },
				sequence: 1
			});
		});
		flushSync(() => {
			handlers.onToolCall?.({
				type: 'tool.call',
				run_id: runId,
				call_id: 'call_1',
				tool: 'testsrv__echo',
				arguments: { text: 'b' },
				sequence: 2
			});
		});

		// Resolve only the second call.
		flushSync(() => {
			handlers.onToolResult?.({
				type: 'tool.result',
				run_id: runId,
				call_id: 'call_1',
				result: 'ok',
				is_error: false,
				sequence: 3
			});
		});

		const chips = Array.from(document.querySelectorAll('[data-status]'));
		expect(chips).toHaveLength(2);
		// First chip (call_0) is still running; second (call_1) is done.
		expect(chips[0].getAttribute('data-status')).toBe('running');
		expect(chips[1].getAttribute('data-status')).toBe('done');

		// Replay of call_0 must not add a third chip (dedupe guard).
		flushSync(() => {
			handlers.onToolCall?.({
				type: 'tool.call',
				run_id: runId,
				call_id: 'call_0',
				tool: 'testsrv__echo',
				arguments: { text: 'a' },
				sequence: 4
			});
		});

		expect(document.querySelectorAll('[data-status]')).toHaveLength(2);

		unmount(app);
		document.body.innerHTML = '';
	});
});
