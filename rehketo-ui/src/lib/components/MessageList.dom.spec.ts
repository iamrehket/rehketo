import { mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';

import MessageList from './MessageList.svelte';
import type { ApprovalItem } from '$lib/types';

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
