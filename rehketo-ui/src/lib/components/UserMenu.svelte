<script lang="ts">
	import { goto } from '$app/navigation';

	import { dismiss } from '$lib/actions/dismiss';
	import { apiFetch } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { conversations } from '$lib/stores/conversations.svelte';

	let open = $state(false);

	let user = $derived(auth.current?.user ?? null);
	let initials = $derived.by(() => {
		const name = user?.display_name ?? user?.email ?? '';
		const parts = name.split(/\s+/).filter(Boolean);
		if (parts.length === 0) return '?';
		if (parts.length === 1) return (parts[0] ?? '?').slice(0, 2).toUpperCase();
		return ((parts[0] ?? '').charAt(0) + (parts[1] ?? '').charAt(0)).toUpperCase();
	});
	let displayLabel = $derived(user?.email ?? user?.display_name ?? 'Account');

	async function logout(): Promise<void> {
		open = false;
		try {
			await apiFetch('/auth/logout', { method: 'POST' });
		} finally {
			auth.clear();
			conversations.clear();
			await goto('/login');
		}
	}
</script>

{#if user}
	<div class="relative" use:dismiss={{ active: open, onDismiss: () => (open = false) }}>
		<button
			type="button"
			onclick={() => (open = !open)}
			class="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-surface"
		>
			<span
				class="flex h-7 w-7 items-center justify-center rounded-full bg-accent text-xs font-semibold text-white"
			>
				{initials}
			</span>
			<span class="flex-1 truncate text-muted">{displayLabel}</span>
		</button>

		{#if open}
			<div
				class="absolute bottom-full left-0 z-20 mb-1 w-full overflow-hidden rounded-md border border-border bg-surface text-sm shadow-lg"
			>
				<button
					type="button"
					onclick={() => {
						open = false;
						void goto('/settings');
					}}
					class="block w-full px-3 py-2 text-left hover:bg-surface-hover"
				>
					Settings
				</button>
				<button
					type="button"
					onclick={logout}
					class="block w-full px-3 py-2 text-left hover:bg-surface-hover"
				>
					Log out
				</button>
			</div>
		{/if}
	</div>
{/if}
