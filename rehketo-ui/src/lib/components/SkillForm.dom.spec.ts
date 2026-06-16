import { flushSync, mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import SkillForm from './SkillForm.svelte';
import type { McpServerOut, MySkillOut } from '$lib/types';

function teardown(app: Record<string, unknown>): void {
	unmount(app);
	document.body.innerHTML = '';
}

describe('SkillForm', () => {
	it('disables submit until name, trigger, and instructions are present (create)', () => {
		const onSubmit = vi.fn();
		const app = mount(SkillForm, {
			target: document.body,
			props: { onSubmit }
		});
		const btn = document.querySelector('[data-action="submit"]') as HTMLButtonElement;
		expect(btn.disabled).toBe(true);
		teardown(app);
	});

	it('makes the name field read-only in edit mode', () => {
		const skill: MySkillOut = {
			id: '1',
			name: 'my-notes',
			display_name: null,
			kind: 'doc',
			trigger: 't',
			instructions: 'i',
			enabled: true,
			source: 'owned',
			editable: true
		};
		const app = mount(SkillForm, {
			target: document.body,
			props: { skill, onSubmit: vi.fn() }
		});
		const nameInput = document.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameInput.readOnly).toBe(true);
		teardown(app);
	});

	it('admin variant shows a kind selector and swaps instructions for a server picker when mcp selected', () => {
		const servers: McpServerOut[] = [
			{
				id: 's1',
				name: 'github',
				url: 'https://x/mcp',
				has_auth_token: false,
				allowed_roles: ['User'],
				enabled: true,
				auto_approve: false,
				created_at: '',
				updated_at: ''
			}
		];
		const app = mount(SkillForm, {
			target: document.body,
			props: { variant: 'admin', servers, onSubmit: vi.fn() }
		});
		// kind selector present (default is doc)
		const kindSelect = document.querySelector('[data-field="kind"]') as HTMLSelectElement;
		expect(kindSelect).not.toBeNull();
		// instructions shown for doc kind
		expect(document.querySelector('[data-field="instructions"]')).not.toBeNull();
		expect(document.querySelector('[data-field="mcp-server"]')).toBeNull();
		// submit disabled (name + trigger + instructions all blank)
		const btn = document.querySelector('[data-action="submit"]') as HTMLButtonElement;
		expect(btn.disabled).toBe(true);
		teardown(app);
	});

	it('admin variant swaps to mcp-server picker when kind is changed to mcp', () => {
		const servers: McpServerOut[] = [
			{
				id: 's1',
				name: 'github',
				url: 'https://x/mcp',
				has_auth_token: false,
				allowed_roles: ['User'],
				enabled: true,
				auto_approve: false,
				created_at: '',
				updated_at: ''
			}
		];
		const app = mount(SkillForm, {
			target: document.body,
			props: { variant: 'admin', servers, onSubmit: vi.fn() }
		});
		const kindSelect = document.querySelector('[data-field="kind"]') as HTMLSelectElement;
		kindSelect.value = 'mcp';
		kindSelect.dispatchEvent(new Event('change', { bubbles: true }));
		flushSync();
		expect(document.querySelector('[data-field="instructions"]')).toBeNull();
		expect(document.querySelector('[data-field="mcp-server"]')).not.toBeNull();
		teardown(app);
	});

	it('create mode: fills form and fires onSubmit with assembled body', () => {
		const onSubmit = vi.fn();
		const app = mount(SkillForm, {
			target: document.body,
			props: { onSubmit }
		});

		(document.querySelector('[data-field="name"]') as HTMLInputElement).value = 'my-notes';
		(document.querySelector('[data-field="name"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		(document.querySelector('[data-field="trigger"]') as HTMLInputElement).value =
			'the user asks about notes';
		(document.querySelector('[data-field="trigger"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		(document.querySelector('[data-field="instructions"]') as HTMLTextAreaElement).value =
			'Use the notes tool.';
		(document.querySelector('[data-field="instructions"]') as HTMLTextAreaElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);

		flushSync();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();

		expect(onSubmit).toHaveBeenCalledTimes(1);
		expect(onSubmit.mock.calls[0][0]).toMatchObject({
			name: 'my-notes',
			display_name: null,
			trigger: 'the user asks about notes',
			instructions: 'Use the notes tool.',
			enabled: true
		});
		teardown(app);
	});

	it('edit mode: Cancel fires onCancel', () => {
		const skill: MySkillOut = {
			id: '2',
			name: 'notes',
			display_name: null,
			kind: 'doc',
			trigger: 't',
			instructions: 'i',
			enabled: true,
			source: 'owned',
			editable: true
		};
		const onCancel = vi.fn();
		const app = mount(SkillForm, {
			target: document.body,
			props: { skill, onSubmit: vi.fn(), onCancel }
		});
		(document.querySelector('[data-action="cancel"]') as HTMLButtonElement).click();
		expect(onCancel).toHaveBeenCalledTimes(1);
		teardown(app);
	});
});
