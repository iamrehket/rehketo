<script lang="ts">
	import { apiFetch } from '$lib/api';
	import McpServerForm from '$lib/components/McpServerForm.svelte';
	import type { McpServerCreateBody, McpServerPatchBody } from '$lib/mcp-server-form';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type McpServerOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	// Snapshot the server-loaded value into local state. data.servers is a one-time initialiser.
	// svelte-ignore state_referenced_locally
	let servers = $state<McpServerOut[]>(data.servers);
	let editingId = $state<string | null>(null);
	// Separate flags: the create form and an open edit form coexist, so an
	// in-flight save must not disable the Add form (and vice versa).
	let createBusy = $state(false);
	let editBusy = $state(false);

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		// 403: apiFetch already fired the global forbidden hook; skip the
		// second toast to avoid duplicates (same pattern as settings page).
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} MCP server.` });
		}
	}

	async function create(body: McpServerCreateBody): Promise<void> {
		createBusy = true;
		try {
			const created = await apiFetch<McpServerOut>('/admin/mcp-servers', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			servers = [created, ...servers];
			toasts.push({ variant: 'info', message: 'MCP server added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			createBusy = false;
		}
	}

	async function save(server: McpServerOut, body: McpServerPatchBody): Promise<void> {
		editBusy = true;
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
			editingId = null;
			toasts.push({ variant: 'info', message: 'MCP server updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			editBusy = false;
		}
	}

	async function toggle(server: McpServerOut): Promise<void> {
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ enabled: !server.enabled })
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}

	async function toggleAutoApprove(server: McpServerOut): Promise<void> {
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ auto_approve: !server.auto_approve })
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}

	async function remove(server: McpServerOut): Promise<void> {
		if (!confirm(`Delete MCP server "${server.name}"?`)) return;
		try {
			await apiFetch(`/admin/mcp-servers/${server.id}`, { method: 'DELETE' });
			servers = servers.filter((s) => s.id !== server.id);
		} catch (err) {
			fail('delete', err);
		}
	}
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">MCP servers</h1>
	<p class="mt-1 text-sm text-muted">
		External tool servers available to agent runs. Granted roles get all of a server's tools; tool
		calls require per-call user approval unless auto-approve is on. Disable to take a server offline
		without deleting it.
	</p>

	<ul class="mt-6 flex flex-col gap-3">
		{#each servers as server (server.id)}
			<li class="rounded-md border border-border bg-surface p-3">
				<div class="flex items-center justify-between gap-3">
					<div>
						<span class="font-mono text-sm">{server.name}</span>
						{#if !server.enabled}
							<span class="ml-2 text-xs text-muted">disabled</span>
						{/if}
						<p class="text-xs text-muted">{server.url}</p>
						<p class="text-xs text-muted">
							roles: {server.allowed_roles.join(', ')}{#if server.has_auth_token}&nbsp;· token set{/if}{#if server.auto_approve}&nbsp;·
								auto-approve{/if}
						</p>
					</div>
					<div class="flex gap-2">
						<button
							type="button"
							data-action="edit"
							onclick={() => (editingId = editingId === server.id ? null : server.id)}
							class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
						>
							Edit
						</button>
						<button
							type="button"
							data-action="toggle"
							onclick={() => toggle(server)}
							class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
						>
							{server.enabled ? 'Disable' : 'Enable'}
						</button>
						<button
							type="button"
							data-action="toggle-auto-approve"
							onclick={() => toggleAutoApprove(server)}
							class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
						>
							{server.auto_approve ? 'Require approval' : 'Auto-approve'}
						</button>
						<button
							type="button"
							data-action="delete"
							onclick={() => remove(server)}
							class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
						>
							Delete
						</button>
					</div>
				</div>
				{#if editingId === server.id}
					<div class="mt-3 border-t border-border pt-3">
						<McpServerForm
							{server}
							busy={editBusy}
							onSubmit={(body) => save(server, body as McpServerPatchBody)}
							onCancel={() => (editingId = null)}
						/>
					</div>
				{/if}
			</li>
		{:else}
			<li class="text-sm text-muted">No servers configured.</li>
		{/each}
	</ul>

	<section class="mt-8 rounded-md border border-border bg-surface p-4">
		<h2 class="text-sm font-semibold">Add server</h2>
		<div class="mt-3">
			<McpServerForm
				server={null}
				busy={createBusy}
				onSubmit={(body) => create(body as McpServerCreateBody)}
			/>
		</div>
	</section>
</div>
