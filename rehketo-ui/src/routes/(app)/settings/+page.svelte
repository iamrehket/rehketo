<script lang="ts">
	import { apiFetch } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type PreferencesOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const MAX_LENGTH = 4000;

	// Snapshot the server-loaded value into local edit state. The page owns
	// the value from here; data.preferences is a one-time initialiser.
	// svelte-ignore state_referenced_locally
	let value = $state(data.preferences.custom_instructions);
	// svelte-ignore state_referenced_locally
	let saved = $state(data.preferences.custom_instructions);
	let saving = $state(false);

	let overLimit = $derived(value.length > MAX_LENGTH);
	let dirty = $derived(value !== saved);

	async function save(): Promise<void> {
		saving = true;
		try {
			const res = await apiFetch<PreferencesOut>('/me/preferences', {
				method: 'PUT',
				body: JSON.stringify({ custom_instructions: value })
			});
			saved = res.custom_instructions;
			toasts.push({ variant: 'info', message: 'Preferences saved.' });
		} catch (err) {
			if (err instanceof ApiError) console.warn('save preferences failed:', err.code, err.message);
			// 403: apiFetch already fired the global forbidden hook (root layout
			// pushes an error toast), so skip the second toast to avoid duplicates.
			if (!(err instanceof ApiError && err.status === 403)) {
				toasts.push({ variant: 'error', message: 'Could not save preferences.' });
			}
		} finally {
			saving = false;
		}
	}
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">Settings</h1>

	<section class="mt-6">
		<label for="custom-instructions" class="text-sm font-semibold">Custom instructions</label>
		<p class="mt-1 text-sm text-muted">Included in every new chat.</p>
		<textarea
			id="custom-instructions"
			bind:value
			rows="8"
			placeholder="How should the assistant behave?"
			aria-describedby="custom-instructions-counter"
			aria-invalid={overLimit || undefined}
			class="mt-3 w-full resize-y rounded-md border border-border bg-surface p-3 text-sm"
		></textarea>
		<div class="mt-2 flex items-center justify-between">
			<span
				id="custom-instructions-counter"
				class="text-xs {overLimit ? 'text-danger' : 'text-muted'}"
			>
				{value.length} / {MAX_LENGTH}
			</span>
			<button
				type="button"
				onclick={save}
				disabled={!dirty || overLimit || saving}
				class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
			>
				Save
			</button>
		</div>
	</section>

	{#if auth.can('admin.manage_mcp_servers')}
		<section class="mt-8">
			<h2 class="text-sm font-semibold">Administration</h2>
			<a href="/settings/mcp-servers" class="mt-2 inline-block text-sm text-accent hover:underline">
				Manage MCP servers →
			</a>
		</section>
	{/if}
</div>
