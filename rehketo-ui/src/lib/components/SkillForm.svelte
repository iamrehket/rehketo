<script lang="ts">
	import {
		buildAdminCreateBody,
		buildAdminPatchBody,
		buildSkillPatchBody,
		type AdminSkillCreateBody,
		type AdminSkillPatchBody,
		type MySkillCreateBody,
		type MySkillPatchBody
	} from '$lib/skill-form';
	import type { AdminSkillOut, McpServerOut, MySkillOut } from '$lib/types';

	// Source of truth for roles: rehketo-api/rehketo/permissions/roles.py.
	const ROLES = ['Admin', 'Moderator', 'User'];

	let {
		skill = null,
		variant = 'user',
		servers = [],
		busy = false,
		onSubmit,
		onCancel
	}: {
		skill?: MySkillOut | AdminSkillOut | null;
		variant?: 'user' | 'admin';
		servers?: McpServerOut[];
		busy?: boolean;
		onSubmit: (
			body: MySkillCreateBody | MySkillPatchBody | AdminSkillCreateBody | AdminSkillPatchBody
		) => void;
		onCancel?: () => void;
	} = $props();

	const isEdit = $derived(skill !== null);
	const isAdmin = $derived(variant === 'admin');
	const uid = $derived(skill ? `skill-${skill.id}` : 'skill');

	// svelte-ignore state_referenced_locally
	let name = $state(skill?.name ?? '');
	// svelte-ignore state_referenced_locally
	let displayName = $state(skill?.display_name ?? '');
	// svelte-ignore state_referenced_locally
	let trigger = $state(skill?.trigger ?? '');
	// svelte-ignore state_referenced_locally
	let instructions = $state(skill?.instructions ?? '');
	// svelte-ignore state_referenced_locally
	let enabled = $state(skill?.enabled ?? true);
	// kind: user authoring is always 'doc'; admin chooses on create, fixed on edit.
	// svelte-ignore state_referenced_locally
	let kind = $state<'doc' | 'mcp'>(skill?.kind ?? 'doc');
	// Admin-only fields — present only on AdminSkillOut.
	// svelte-ignore state_referenced_locally
	let allowedRoles = $state<string[]>(
		skill && 'allowed_roles' in skill ? [...skill.allowed_roles] : [...ROLES]
	);
	// svelte-ignore state_referenced_locally
	let mcpServerId = $state(skill && 'mcp_server_id' in skill ? (skill.mcp_server_id ?? '') : '');

	const isDoc = $derived(kind === 'doc');

	function submit(): void {
		if (isAdmin) {
			const state = {
				name,
				kind,
				displayName,
				trigger,
				instructions,
				mcpServerId,
				allowedRoles,
				enabled
			};
			onSubmit(skill ? buildAdminPatchBody(state) : buildAdminCreateBody(state));
		} else if (skill) {
			onSubmit(buildSkillPatchBody({ displayName, trigger, instructions, enabled }));
		} else {
			onSubmit({ name, display_name: displayName || null, trigger, instructions, enabled });
		}
	}

	const canSubmit = $derived.by(() => {
		if (!isEdit && !name) return false;
		if (!trigger) return false;
		return isDoc ? Boolean(instructions) : Boolean(mcpServerId);
	});
</script>

<div class="flex flex-col gap-3">
	<label class="text-xs text-muted" for={`${uid}-name`}>Name</label>
	<input
		id={`${uid}-name`}
		data-field="name"
		bind:value={name}
		readonly={isEdit}
		placeholder={isEdit ? undefined : 'my-notes'}
		class="rounded-md border border-border p-2 text-sm {isEdit ? 'bg-surface text-muted' : 'bg-bg'}"
	/>

	{#if isAdmin}
		<label class="text-xs text-muted" for={`${uid}-kind`}>Kind</label>
		{#if isEdit}
			<span data-field="kind" class="font-mono text-sm text-muted">{kind}</span>
		{:else}
			<select
				id={`${uid}-kind`}
				data-field="kind"
				bind:value={kind}
				class="rounded-md border border-border bg-bg p-2 text-sm"
			>
				<option value="doc">doc</option>
				<option value="mcp">mcp</option>
			</select>
		{/if}
	{/if}

	<label class="text-xs text-muted" for={`${uid}-display`}>Display name (optional)</label>
	<input
		id={`${uid}-display`}
		data-field="display-name"
		bind:value={displayName}
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	<label class="text-xs text-muted" for={`${uid}-trigger`}>Use when…</label>
	<input
		id={`${uid}-trigger`}
		data-field="trigger"
		bind:value={trigger}
		placeholder="the user asks about my project notes"
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	{#if isDoc}
		<label class="text-xs text-muted" for={`${uid}-instructions`}>Instructions</label>
		<textarea
			id={`${uid}-instructions`}
			data-field="instructions"
			bind:value={instructions}
			rows="6"
			class="resize-y rounded-md border border-border bg-bg p-2 text-sm"
		></textarea>
	{:else}
		<label class="text-xs text-muted" for={`${uid}-server`}>MCP server</label>
		<select
			id={`${uid}-server`}
			data-field="mcp-server"
			bind:value={mcpServerId}
			class="rounded-md border border-border bg-bg p-2 text-sm"
		>
			<option value="">— choose a server —</option>
			{#each servers as srv (srv.id)}
				<option value={srv.id}>{srv.name}</option>
			{/each}
		</select>
	{/if}

	{#if isAdmin}
		<fieldset class="flex gap-4 text-sm">
			<legend class="text-xs text-muted">Allowed roles</legend>
			{#each ROLES as role (role)}
				<label class="flex items-center gap-1">
					<input type="checkbox" value={role} bind:group={allowedRoles} />
					{role}
				</label>
			{/each}
		</fieldset>
	{/if}

	<label class="flex items-center gap-2 text-sm">
		<input data-field="enabled" type="checkbox" bind:checked={enabled} />
		Enabled
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
			type="button"
			data-action="submit"
			onclick={submit}
			disabled={busy || !canSubmit}
			class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
		>
			{isEdit ? 'Save' : 'Add skill'}
		</button>
	</div>
</div>
