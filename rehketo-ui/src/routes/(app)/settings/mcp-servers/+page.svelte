<script lang="ts">
	import { apiFetch } from '$lib/api';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type McpServerOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const ROLES = ['Admin', 'Moderator', 'User'];

	// svelte-ignore state_referenced_locally
	let servers = $state<McpServerOut[]>(data.servers);

	let name = $state('');
	let url = $state('');
	let authToken = $state('');
	let allowedRoles = $state<string[]>([...ROLES]);
	let busy = $state(false);

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		// 403: apiFetch already fired the global forbidden hook; skip the
		// second toast to avoid duplicates (same pattern as settings page).
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} MCP server.` });
		}
	}

	async function create(): Promise<void> {
		busy = true;
		try {
			const created = await apiFetch<McpServerOut>('/admin/mcp-servers', {
				method: 'POST',
				body: JSON.stringify({
					name,
					url,
					auth_token: authToken || null,
					allowed_roles: allowedRoles,
					enabled: true
				})
			});
			servers = [created, ...servers];
			name = '';
			url = '';
			authToken = '';
			toasts.push({ variant: 'info', message: 'MCP server added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			busy = false;
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
		External tool servers available to agent runs. Granted roles get all of a server's tools;
		disable to take a server offline without deleting it.
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
							roles: {server.allowed_roles.join(', ')}{#if server.has_auth_token}&nbsp;· token set{/if}
						</p>
					</div>
					<div class="flex gap-2">
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
							data-action="delete"
							onclick={() => remove(server)}
							class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
						>
							Delete
						</button>
					</div>
				</div>
			</li>
		{:else}
			<li class="text-sm text-muted">No servers configured.</li>
		{/each}
	</ul>

	<section class="mt-8 rounded-md border border-border bg-surface p-4">
		<h2 class="text-sm font-semibold">Add server</h2>
		<div class="mt-3 flex flex-col gap-3">
			<label class="text-xs text-muted" for="mcp-name">Name (tool prefix)</label>
			<input
				id="mcp-name"
				bind:value={name}
				placeholder="github"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<label class="text-xs text-muted" for="mcp-url">URL</label>
			<input
				id="mcp-url"
				bind:value={url}
				placeholder="https://host/mcp"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<label class="text-xs text-muted" for="mcp-token">Bearer token (optional, write-only)</label>
			<input
				id="mcp-token"
				bind:value={authToken}
				type="password"
				autocomplete="off"
				class="rounded-md border border-border bg-bg p-2 text-sm"
			/>
			<fieldset class="flex gap-4 text-sm">
				<legend class="text-xs text-muted">Allowed roles</legend>
				{#each ROLES as role (role)}
					<label class="flex items-center gap-1">
						<input type="checkbox" value={role} bind:group={allowedRoles} />
						{role}
					</label>
				{/each}
			</fieldset>
			<button
				id="mcp-create"
				type="button"
				onclick={create}
				disabled={busy || !name || !url}
				class="self-end rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
			>
				Add
			</button>
		</div>
	</section>
</div>
