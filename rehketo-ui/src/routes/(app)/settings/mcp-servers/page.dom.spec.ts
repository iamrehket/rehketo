// MCP servers admin page: renders the loaded server list, creates via POST,
// toggles enabled via PATCH, and deletes via DELETE. $lib/api is mocked — no network.

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Page from './+page.svelte';
import { apiFetch } from '$lib/api';
import { toasts } from '$lib/stores/toasts.svelte';
import type { McpServerOut } from '$lib/types';

// Keep a typed reference to the mock so edit-suite beforeEach can call mockReset.
const apiFetchMock = vi.mocked(apiFetch);

vi.mock('$lib/api', () => ({ apiFetch: vi.fn() }));

function server(overrides: Partial<McpServerOut> = {}): McpServerOut {
	return {
		id: 's0000000-0000-0000-0000-000000000001',
		name: 'github',
		url: 'https://mcp.example.com/mcp',
		has_auth_token: true,
		allowed_roles: ['Admin', 'User'],
		enabled: true,
		auto_approve: false,
		created_at: '2026-06-11T00:00:00Z',
		updated_at: '2026-06-11T00:00:00Z',
		...overrides
	};
}

describe('MCP servers admin page', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
		for (const t of [...toasts.items]) toasts.dismiss(t.id);
	});

	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('renders the loaded server list', () => {
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [server()] } }
		});
		expect(document.body.textContent).toContain('github');
		expect(document.body.textContent).toContain('https://mcp.example.com/mcp');
		unmount(app);
	});

	it('shows empty state when no servers configured', () => {
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [] } }
		});
		expect(document.body.textContent).toContain('No servers configured');
		unmount(app);
	});

	it('creates a server via POST and prepends it to the list', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ name: 'newsrv' }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [] } }
		});

		const nameInput = document.querySelector('#mcp-name') as HTMLInputElement;
		nameInput.value = 'newsrv';
		nameInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		const urlInput = document.querySelector('#mcp-url') as HTMLInputElement;
		urlInput.value = 'https://new.example.com/mcp';
		urlInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		(document.querySelector('#mcp-create') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers',
				expect.objectContaining({ method: 'POST' })
			);
			expect(document.body.textContent).toContain('newsrv');
		});
		unmount(app);
	});

	it('toggles enabled via PATCH', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ enabled: false }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [server()] } }
		});
		(document.querySelector('[data-action="toggle"]') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers/s0000000-0000-0000-0000-000000000001',
				expect.objectContaining({ method: 'PATCH' })
			);
		});
		unmount(app);
	});

	it('deletes a server when confirm returns true', async () => {
		vi.stubGlobal(
			'confirm',
			vi.fn(() => true)
		);
		vi.mocked(apiFetch).mockResolvedValueOnce(undefined);
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [server()] } }
		});

		expect(document.body.textContent).toContain('github');
		(document.querySelector('[data-action="delete"]') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers/s0000000-0000-0000-0000-000000000001',
				expect.objectContaining({ method: 'DELETE' })
			);
			expect(document.body.textContent).not.toContain('github');
		});
		unmount(app);
	});

	it('does not delete a server when confirm returns false', async () => {
		vi.stubGlobal(
			'confirm',
			vi.fn(() => false)
		);
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [server()] } }
		});

		expect(document.body.textContent).toContain('github');
		(document.querySelector('[data-action="delete"]') as HTMLButtonElement).click();
		// Give a tick for any async work that might (incorrectly) run
		await new Promise((r) => setTimeout(r, 0));
		expect(apiFetch).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain('github');
		unmount(app);
	});

	it('create sends auto_approve in POST body', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ name: 'newsrv', auto_approve: true }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [] } }
		});

		const nameInput = document.querySelector('#mcp-name') as HTMLInputElement;
		nameInput.value = 'newsrv';
		nameInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		const urlInput = document.querySelector('#mcp-url') as HTMLInputElement;
		urlInput.value = 'https://new.example.com/mcp';
		urlInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		(document.querySelector('#mcp-auto-approve') as HTMLInputElement).click();
		flushSync();

		(document.querySelector('#mcp-create') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers',
				expect.objectContaining({ method: 'POST' })
			);
			const body = JSON.parse((vi.mocked(apiFetch).mock.calls[0][1] as { body: string }).body);
			expect(body.auto_approve).toBe(true);
		});
		unmount(app);
	});

	it('row toggle PATCHes auto_approve', async () => {
		vi.mocked(apiFetch).mockResolvedValueOnce(server({ auto_approve: true }));
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [server({ auto_approve: false })] } }
		});
		(document.querySelector('[data-action="toggle-auto-approve"]') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(apiFetch).toHaveBeenCalledWith(
				'/admin/mcp-servers/s0000000-0000-0000-0000-000000000001',
				expect.objectContaining({ method: 'PATCH' })
			);
			const body = JSON.parse((vi.mocked(apiFetch).mock.calls[0][1] as { body: string }).body);
			expect(body).toEqual({ auto_approve: true });
		});
		unmount(app);
	});

	it('pushes an error toast when create fails', async () => {
		const { ApiError } = await import('$lib/types');
		vi.mocked(apiFetch).mockRejectedValueOnce(
			new ApiError({ code: 'validation_error', message: 'bad name', status: 422 })
		);
		const app = mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers: [] } }
		});

		const nameInput = document.querySelector('#mcp-name') as HTMLInputElement;
		nameInput.value = 'newsrv';
		nameInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		const urlInput = document.querySelector('#mcp-url') as HTMLInputElement;
		urlInput.value = 'https://new.example.com/mcp';
		urlInput.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		(document.querySelector('#mcp-create') as HTMLButtonElement).click();
		await vi.waitFor(() => {
			expect(toasts.items.some((t) => t.variant === 'error')).toBe(true);
		});
		unmount(app);
	});
});

describe('MCP servers page — edit', () => {
	function editServer(overrides: Partial<McpServerOut> = {}): McpServerOut {
		return {
			id: 'srv-1',
			name: 'github',
			url: 'https://host/mcp',
			has_auth_token: true,
			allowed_roles: ['Admin'],
			enabled: true,
			auto_approve: false,
			created_at: '2026-06-13T00:00:00Z',
			updated_at: '2026-06-13T00:00:00Z',
			...overrides
		};
	}

	function mountPage(servers: McpServerOut[]) {
		return mount(Page, {
			target: document.body,
			props: { data: { authenticated: true, servers } }
		});
	}

	beforeEach(() => {
		// Self-contained: don't depend on the outer suite's cleanup running first.
		vi.clearAllMocks();
		vi.unstubAllGlobals();
		apiFetchMock.mockReset();
		for (const t of [...toasts.items]) toasts.dismiss(t.id);
	});

	afterEach(() => {
		document.body.innerHTML = '';
	});

	it('expands a row into an edit form when Edit is clicked', () => {
		const app = mountPage([editServer()]);
		// Before clicking Edit, no edit form exists inside the list row.
		const list = document.querySelector('ul') as HTMLUListElement;
		expect(list.querySelector('[data-field="url"]')).toBeNull();
		(document.querySelector('[data-action="edit"]') as HTMLButtonElement).click();
		flushSync();
		const nameEl = list.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameEl.readOnly).toBe(true);
		expect(nameEl.value).toBe('github');
		unmount(app);
	});

	it('opens only one editor at a time', () => {
		const app = mountPage([
			editServer({ id: 'a', name: 'aaa' }),
			editServer({ id: 'b', name: 'bbb' })
		]);
		const list = document.querySelector('ul') as HTMLUListElement;
		const editButtons = document.querySelectorAll('[data-action="edit"]');
		(editButtons[0] as HTMLButtonElement).click();
		flushSync();
		(editButtons[1] as HTMLButtonElement).click();
		flushSync();
		// Only one edit form open inside the list at a time, and it's the second row's.
		const openNames = list.querySelectorAll('[data-field="name"]');
		expect(openNames.length).toBe(1);
		expect((openNames[0] as HTMLInputElement).value).toBe('bbb');
		unmount(app);
	});

	it('PATCHes on save and updates the row', async () => {
		apiFetchMock.mockResolvedValue(editServer({ url: 'https://new/mcp' }));
		const app = mountPage([editServer()]);
		(document.querySelector('[data-action="edit"]') as HTMLButtonElement).click();
		flushSync();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		await Promise.resolve();
		expect(apiFetch).toHaveBeenCalledWith(
			'/admin/mcp-servers/srv-1',
			expect.objectContaining({ method: 'PATCH' })
		);
		unmount(app);
	});

	it('an in-flight save does not disable the create form', () => {
		// A save that never resolves keeps the edit form's busy flag set.
		apiFetchMock.mockReturnValue(new Promise(() => {}));
		const app = mountPage([editServer()]);

		// Fill the always-present create form so its Add button is gated only by busy.
		const createName = document.querySelector('#mcp-name') as HTMLInputElement;
		createName.value = 'newsrv';
		createName.dispatchEvent(new Event('input', { bubbles: true }));
		const createUrl = document.querySelector('#mcp-url') as HTMLInputElement;
		createUrl.value = 'https://new.example.com/mcp';
		createUrl.dispatchEvent(new Event('input', { bubbles: true }));
		flushSync();

		// Open the edit form and start a save that stays pending.
		(document.querySelector('[data-action="edit"]') as HTMLButtonElement).click();
		flushSync();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		flushSync();

		// The Add button must stay enabled despite the in-flight edit save.
		expect((document.querySelector('#mcp-create') as HTMLButtonElement).disabled).toBe(false);
		unmount(app);
	});
});
