import { mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import ApprovalCard from './ApprovalCard.svelte';
import type { ApprovalItem } from '$lib/types';

function item(overrides: Partial<ApprovalItem> = {}): ApprovalItem {
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

describe('ApprovalCard', () => {
	it('renders pending state with approve/deny buttons when decidable', () => {
		const onDecide = vi.fn();
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item(), canDecide: true, onDecide }
		});
		expect(document.body.textContent).toContain('testsrv__echo');
		expect(document.querySelector('[data-decision="pending"]')).not.toBeNull();
		(document.querySelector('[data-action="approve"]') as HTMLButtonElement).click();
		expect(onDecide).toHaveBeenCalledWith('approve');
		(document.querySelector('[data-action="deny"]') as HTMLButtonElement).click();
		expect(onDecide).toHaveBeenCalledWith('deny');
		unmount(app);
		document.body.innerHTML = '';
	});

	it('hides buttons without decide capability', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item(), canDecide: false }
		});
		expect(document.querySelector('[data-action="approve"]')).toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('renders approved state without buttons', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item({ decision: 'approve' }), canDecide: true }
		});
		expect(document.querySelector('[data-decision="approve"]')).not.toBeNull();
		expect(document.querySelector('[data-action="approve"]')).toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('renders denied state', () => {
		const app = mount(ApprovalCard, {
			target: document.body,
			props: { item: item({ decision: 'deny' }), canDecide: true }
		});
		expect(document.querySelector('[data-decision="deny"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});
