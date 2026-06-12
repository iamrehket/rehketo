import { mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';

import MessageList from './MessageList.svelte';
import type { ApprovalItem, MessageItem, ToolCallItem } from '$lib/types';

function approvalItem(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
	return {
		kind: 'approval',
		run_id: 'run-1',
		approval_id: 'ap-1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		decision: null,
		created_at: '2026-06-12T00:00:00Z',
		...overrides
	};
}

describe('MessageList', () => {
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

	it('withholds approval buttons for a non-live run', () => {
		const app = mount(MessageList, {
			target: document.body,
			props: { items: [approvalItem()], liveRunId: null, canDecide: true }
		});
		expect(document.querySelector('[data-decision="pending"]')).not.toBeNull();
		expect(document.querySelector('[data-action="approve"]')).toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('offers approval buttons for the live run', () => {
		const app = mount(MessageList, {
			target: document.body,
			props: { items: [approvalItem()], liveRunId: 'run-1', canDecide: true }
		});
		expect(document.querySelector('[data-action="approve"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});
