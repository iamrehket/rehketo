import { mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';

import ToolChip from './ToolChip.svelte';
import type { ToolCallItem } from '$lib/types';

function item(overrides: Partial<ToolCallItem> = {}): ToolCallItem {
	return {
		kind: 'tool',
		run_id: 'run-1',
		call_id: 'c1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		result: null,
		is_error: null,
		created_at: '2026-06-11T00:00:00Z',
		...overrides
	};
}

describe('ToolChip', () => {
	it('shows running state while pending on a live run', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item(), live: true }
		});
		expect(document.body.textContent).toContain('testsrv__echo');
		expect(document.querySelector('[data-status="running"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('shows incomplete state while pending on a dead run', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item(), live: false }
		});
		expect(document.querySelector('[data-status="incomplete"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});

	it('shows success state and result when present', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item({ result: 'echo: hi', is_error: false }) }
		});
		expect(document.querySelector('[data-status="done"]')).not.toBeNull();
		expect(document.body.textContent).toContain('echo: hi');
		unmount(app);
		document.body.innerHTML = '';
	});

	it('shows error state when is_error', () => {
		const app = mount(ToolChip, {
			target: document.body,
			props: { item: item({ result: 'boom', is_error: true }) }
		});
		expect(document.querySelector('[data-status="error"]')).not.toBeNull();
		unmount(app);
		document.body.innerHTML = '';
	});
});
