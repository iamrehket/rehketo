// MCP servers admin page: renders the loaded server list, creates via POST,
// toggles enabled via PATCH, and deletes via DELETE. $lib/api is mocked — no network.

import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import Page from './+page.svelte';
import { apiFetch } from '$lib/api';
import { toasts } from '$lib/stores/toasts.svelte';
import type { McpServerOut } from '$lib/types';

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
