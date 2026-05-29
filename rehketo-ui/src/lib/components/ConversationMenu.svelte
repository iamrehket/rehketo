<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';

	import { dismiss } from '$lib/actions/dismiss';
	import { apiFetch } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { conversations } from '$lib/stores/conversations.svelte';
	import type { ConversationSummary } from '$lib/types';

	let { conversation }: { conversation: ConversationSummary } = $props();

	let open = $state(false);
	let renaming = $state(false);
	let draftTitle = $state('');

	function close(): void {
		open = false;
	}

	function startRename(): void {
		draftTitle = conversation.title ?? '';
		renaming = true;
		open = false;
	}

	async function submitRename(): Promise<void> {
		const next = draftTitle.trim();
		if (!next || next === conversation.title) {
			renaming = false;
			return;
		}
		await apiFetch(`/conversations/${conversation.id}`, {
			method: 'PATCH',
			body: JSON.stringify({ title: next })
		});
		conversations.patchTitle(conversation.id, next);
		conversations.bumpUpdatedAt(conversation.id);
		renaming = false;
	}

	async function archive(): Promise<void> {
		open = false;
		await apiFetch(`/conversations/${conversation.id}`, { method: 'DELETE' });
		conversations.remove(conversation.id);
		if (page.url.pathname === `/c/${conversation.id}`) await goto('/');
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Enter') void submitRename();
		else if (e.key === 'Escape') renaming = false;
	}
</script>

{#if renaming}
	<input
		type="text"
		bind:value={draftTitle}
		onblur={submitRename}
		onkeydown={onKeydown}
		class="w-full rounded bg-surface-hover px-2 py-1 text-sm text-text ring-1 ring-accent outline-none"
	/>
{:else}
	<div class="relative inline-block" use:dismiss={{ active: open, onDismiss: close }}>
		<button
			type="button"
			onclick={() => (open = !open)}
			class="rounded p-1 text-muted opacity-0 transition-opacity group-hover:opacity-100 hover:bg-surface-hover hover:text-text"
			aria-label="Conversation actions"
		>
			⋯
		</button>
		{#if open}
			<div
				class="absolute top-full right-0 z-20 mt-1 w-40 overflow-hidden rounded-md border border-border bg-surface text-sm shadow-lg"
			>
				{#if auth.can('chat.rename_conversation')}
					<button
						type="button"
						onclick={startRename}
						class="block w-full px-3 py-2 text-left hover:bg-surface-hover"
					>
						Rename
					</button>
				{/if}
				{#if auth.can('chat.delete_conversation')}
					<button
						type="button"
						onclick={archive}
						class="block w-full px-3 py-2 text-left text-danger hover:bg-surface-hover"
					>
						Archive
					</button>
				{/if}
			</div>
		{/if}
	</div>
{/if}
