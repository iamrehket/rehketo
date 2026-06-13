import { flushSync, mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import McpServerForm from './McpServerForm.svelte';
import type { McpServerOut } from '$lib/types';

function server(overrides: Partial<McpServerOut> = {}): McpServerOut {
	return {
		id: 'srv-1',
		name: 'github',
		url: 'https://host/mcp',
		has_auth_token: true,
		allowed_roles: ['Admin', 'Moderator'],
		enabled: true,
		auto_approve: false,
		created_at: '2026-06-13T00:00:00Z',
		updated_at: '2026-06-13T00:00:00Z',
		...overrides
	};
}

function teardown(app: Record<string, unknown>): void {
	unmount(app);
	document.body.innerHTML = '';
}

describe('McpServerForm', () => {
	it('create mode: editable name, no remove-token checkbox, submits a full create body', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: null, busy: false, onSubmit }
		});

		(document.querySelector('[data-field="name"]') as HTMLInputElement).value = 'github';
		(document.querySelector('[data-field="name"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		(document.querySelector('[data-field="url"]') as HTMLInputElement).value = 'https://h/mcp';
		(document.querySelector('[data-field="url"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		expect(document.querySelector('[data-field="remove-token"]')).toBeNull();

		// Apply the pending effect so the submit button's disabled gate reflects
		// the just-entered name/url before we click it.
		flushSync();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		expect(onSubmit).toHaveBeenCalledTimes(1);
		expect(onSubmit.mock.calls[0][0]).toMatchObject({
			name: 'github',
			url: 'https://h/mcp',
			auth_token: null,
			enabled: true,
			auto_approve: false
		});
		teardown(app);
	});

	it('edit mode: name is read-only and prefilled', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server(), busy: false, onSubmit, onCancel: vi.fn() }
		});
		const nameEl = document.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameEl.readOnly).toBe(true);
		expect(nameEl.value).toBe('github');
		teardown(app);
	});

	it('edit mode with a token: shows remove checkbox; checking it sends auth_token null', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server({ has_auth_token: true }), busy: false, onSubmit, onCancel: vi.fn() }
		});
		const remove = document.querySelector('[data-field="remove-token"]') as HTMLInputElement;
		expect(remove).not.toBeNull();
		remove.click();
		// Apply the pending effect before clicking submit (same reason as create test).
		flushSync();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		expect(onSubmit.mock.calls[0][0]).toMatchObject({ auth_token: null });
		teardown(app);
	});

	it('edit mode without a token: no remove checkbox', () => {
		const app = mount(McpServerForm, {
			target: document.body,
			props: {
				server: server({ has_auth_token: false }),
				busy: false,
				onSubmit: vi.fn(),
				onCancel: vi.fn()
			}
		});
		expect(document.querySelector('[data-field="remove-token"]')).toBeNull();
		teardown(app);
	});

	it('edit mode: Cancel fires onCancel', () => {
		const onCancel = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server(), busy: false, onSubmit: vi.fn(), onCancel }
		});
		(document.querySelector('[data-action="cancel"]') as HTMLButtonElement).click();
		expect(onCancel).toHaveBeenCalledTimes(1);
		teardown(app);
	});
});
