<script lang="ts">
	import {
		buildPatchBody,
		type McpServerCreateBody,
		type McpServerPatchBody
	} from '$lib/mcp-server-form';
	import type { McpServerOut } from '$lib/types';

	// Source of truth for roles: rehketo-api/rehketo/permissions/roles.py.
	const ROLES = ['Admin', 'Moderator', 'User'];

	let {
		server = null,
		busy = false,
		onSubmit,
		onCancel
	}: {
		server?: McpServerOut | null;
		busy?: boolean;
		onSubmit: (body: McpServerCreateBody | McpServerPatchBody) => void;
		onCancel?: () => void;
	} = $props();

	// Reactive so that `{#if isEdit}` blocks update if `server` prop changes.
	const isEdit = $derived(server !== null);

	// Input ids must be unique per instance: an open edit form and the always-present
	// create form coexist on the page, so a shared id would duplicate in the DOM and
	// mis-wire `<label for>`. Create keeps the bare `mcp-*` ids the page tests target.
	const uid = $derived(server ? `mcp-${server.id}` : 'mcp');

	// `server` is a one-time initialiser: each form instance edits one row.
	// svelte-ignore state_referenced_locally
	let name = $state(server?.name ?? '');
	// svelte-ignore state_referenced_locally
	let url = $state(server?.url ?? '');
	let authToken = $state('');
	let removeToken = $state(false);
	// svelte-ignore state_referenced_locally
	let allowedRoles = $state<string[]>(server ? [...server.allowed_roles] : [...ROLES]);
	// svelte-ignore state_referenced_locally
	let autoApprove = $state(server?.auto_approve ?? false);

	function submit(): void {
		if (server) {
			onSubmit(buildPatchBody({ url, authToken, removeToken, allowedRoles, autoApprove }));
		} else {
			onSubmit({
				name,
				url,
				auth_token: authToken || null,
				allowed_roles: allowedRoles,
				enabled: true,
				auto_approve: autoApprove
			});
		}
	}

	// Mirror the create page's existing gate: don't let an obviously-incomplete
	// form be submitted. Name is fixed in edit mode, so only URL is required there.
	const canSubmit = $derived(isEdit ? Boolean(url) : Boolean(name) && Boolean(url));
</script>

<div class="flex flex-col gap-3">
	<label class="text-xs text-muted" for={`${uid}-name`}>Name (tool prefix)</label>
	<!-- Name is the immutable tool prefix: editable on create, read-only on edit. -->
	<input
		id={`${uid}-name`}
		data-field="name"
		bind:value={name}
		readonly={isEdit}
		placeholder={isEdit ? undefined : 'github'}
		class="rounded-md border border-border p-2 text-sm {isEdit ? 'bg-surface text-muted' : 'bg-bg'}"
	/>

	<label class="text-xs text-muted" for={`${uid}-url`}>URL</label>
	<input
		id={`${uid}-url`}
		data-field="url"
		bind:value={url}
		placeholder="https://host/mcp"
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	<label class="text-xs text-muted" for={`${uid}-token`}>
		Bearer token{isEdit ? '' : ' (optional, write-only)'}
	</label>
	<input
		id={`${uid}-token`}
		data-field="token"
		bind:value={authToken}
		type="password"
		autocomplete="off"
		placeholder={isEdit && server?.has_auth_token ? 'leave blank to keep current token' : ''}
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>
	{#if isEdit && server?.has_auth_token}
		<label class="flex items-center gap-2 text-sm">
			<input
				data-field="remove-token"
				type="checkbox"
				bind:checked={removeToken}
				disabled={Boolean(authToken)}
			/>
			Remove existing token
		</label>
	{/if}

	<fieldset class="flex gap-4 text-sm">
		<legend class="text-xs text-muted">Allowed roles</legend>
		{#each ROLES as role (role)}
			<label class="flex items-center gap-1">
				<input type="checkbox" value={role} bind:group={allowedRoles} />
				{role}
			</label>
		{/each}
	</fieldset>

	<label class="flex items-center gap-2 text-sm">
		<input
			id={isEdit ? undefined : 'mcp-auto-approve'}
			type="checkbox"
			bind:checked={autoApprove}
		/>
		Auto-approve tool calls (trusted server — skips per-call user approval)
	</label>

	<div class="flex justify-end gap-2">
		{#if isEdit}
			<button
				type="button"
				data-action="cancel"
				onclick={() => onCancel?.()}
				class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-hover"
			>
				Cancel
			</button>
		{/if}
		<button
			id={isEdit ? undefined : 'mcp-create'}
			type="button"
			data-action="submit"
			onclick={submit}
			disabled={busy || !canSubmit}
			class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
		>
			{isEdit ? 'Save' : 'Add'}
		</button>
	</div>
</div>
