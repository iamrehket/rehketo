import { flushSync, mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';

import WorkingBlock from './WorkingBlock.svelte';
import type { WorkingEntry } from '$lib/transcript';
import type { ApprovalItem } from '$lib/types';

function textEntry(text = 'thinking…'): WorkingEntry {
	return { kind: 'text', text };
}

function approvalEntry(overrides: Partial<ApprovalItem> = {}): WorkingEntry {
	return {
		kind: 'approval',
		item: {
			kind: 'approval',
			run_id: 'run-1',
			approval_id: 'ap-1',
			tool: 'testsrv__echo',
			arguments: { text: 'hi' },
			decision: null,
			created_at: '2026-06-12T00:00:00Z',
			...overrides
		}
	};
}

describe('WorkingBlock', () => {
	it('is open when live', () => {
		const app = mount(WorkingBlock, {
			target: document.body,
			props: { entries: [textEntry()], live: true }
		});
		const details = document.querySelector('[data-working]') as HTMLDetailsElement;
		expect(details.open).toBe(true);
		unmount(app);
		document.body.innerHTML = '';
	});

	it('is closed when not live and no pending approval', () => {
		const app = mount(WorkingBlock, {
			target: document.body,
			props: { entries: [textEntry()], live: false }
		});
		const details = document.querySelector('[data-working]') as HTMLDetailsElement;
		expect(details.open).toBe(false);
		unmount(app);
		document.body.innerHTML = '';
	});

	it('user collapse sticks after a props-driven re-render while live', () => {
		// Use a plain mutable object with getters so Svelte re-reads props on
		// each flushSync without needing $state (forbidden in .ts files).
		const state = { entries: [textEntry('step 1')], live: true };
		const app = mount(WorkingBlock, {
			target: document.body,
			props: {
				get entries() {
					return state.entries;
				},
				get live() {
					return state.live;
				}
			}
		});

		const details = document.querySelector('[data-working]') as HTMLDetailsElement;
		expect(details.open).toBe(true);

		// Simulate the user collapsing the block.
		details.open = false;
		details.dispatchEvent(new Event('toggle'));
		flushSync();

		expect(details.open).toBe(false);

		// Simulate a new entry arriving (props-driven re-render).
		flushSync(() => {
			state.entries = [...state.entries, textEntry('step 2')];
		});

		// The block must stay collapsed — the user's choice wins.
		expect(details.open).toBe(false);

		unmount(app);
		document.body.innerHTML = '';
	});

	it('pending approval forces open even after user collapse attempt', () => {
		const app = mount(WorkingBlock, {
			target: document.body,
			props: { entries: [approvalEntry()], live: false }
		});

		const details = document.querySelector('[data-working]') as HTMLDetailsElement;
		// Pending approval should start open.
		expect(details.open).toBe(true);

		// Simulate user collapse attempt.
		details.open = false;
		details.dispatchEvent(new Event('toggle'));
		flushSync();

		// Must remain open — decision buttons are inside.
		expect(details.open).toBe(true);

		unmount(app);
		document.body.innerHTML = '';
	});

	it('user expand sticks after run ends (live flips false)', () => {
		const state = { live: false };
		const app = mount(WorkingBlock, {
			target: document.body,
			props: {
				entries: [textEntry()],
				get live() {
					return state.live;
				}
			}
		});

		const details = document.querySelector('[data-working]') as HTMLDetailsElement;
		expect(details.open).toBe(false);

		// User opens the finished block.
		details.open = true;
		details.dispatchEvent(new Event('toggle'));
		flushSync();

		expect(details.open).toBe(true);

		// live cycles on/off (another run starts then ends on the same instance).
		flushSync(() => {
			state.live = true;
		});
		flushSync(() => {
			state.live = false;
		});

		// userToggled=true wins — block stays open.
		expect(details.open).toBe(true);

		unmount(app);
		document.body.innerHTML = '';
	});
});
