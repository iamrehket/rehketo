// Resume-on-open: ChatView must reattach to an in-flight run named by the
// conversation GET (active_run_id) — and must not open a stream when there
// is none. Mounted with Svelte's own mount() in the jsdom project; $lib/sse
// is mocked so no EventSource is involved.

import { mount, unmount } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatView from './ChatView.svelte';
import { subscribeRun } from '$lib/sse';
import type { ConversationDetail } from '$lib/types';

vi.mock('$lib/sse', () => ({
	subscribeRun: vi.fn(() => ({ state: 'idle', unsubscribe: vi.fn() }))
}));

function conversation(activeRunId: string | null): ConversationDetail {
	return {
		id: 'c0000000-0000-0000-0000-000000000001',
		title: null,
		created_at: '2026-06-10T00:00:00Z',
		updated_at: '2026-06-10T00:00:00Z',
		messages: [],
		active_run_id: activeRunId
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
});
