<script lang="ts">
	import { page } from '$app/state';

	import ConversationMenu from './ConversationMenu.svelte';
	import type { ConversationSummary } from '$lib/types';

	let { conversation }: { conversation: ConversationSummary } = $props();

	let isActive = $derived(page.url.pathname === `/c/${conversation.id}`);
	let displayTitle = $derived(conversation.title?.trim() || 'New chat');
</script>

<!-- The actions menu is a sibling of the link, never a child: interactive
     controls inside an <a> are invalid HTML and made menu clicks navigate to
     the conversation (the parent anchor's default action firing on bubble). -->
<div
	class="group flex items-center rounded-md pr-1 text-sm transition-colors {isActive
		? 'bg-surface-hover text-text'
		: 'text-muted hover:bg-surface hover:text-text'}"
>
	<a href={`/c/${conversation.id}`} class="min-w-0 flex-1 truncate px-2 py-1.5">
		{displayTitle}
	</a>
	<ConversationMenu {conversation} />
</div>
