<script lang="ts">
	import AssistantBubble from './AssistantBubble.svelte';
	import ApprovalCard from './ApprovalCard.svelte';
	import MessageBubble from './MessageBubble.svelte';
	import ToolChip from './ToolChip.svelte';
	import type { ApprovalItem, RunStatus, TranscriptItem } from '$lib/types';

	let {
		items,
		liveRunId = null,
		streamingText = null,
		streamingStatus = null,
		canDecide = false,
		onDecide
	}: {
		items: TranscriptItem[];
		liveRunId?: string | null;
		streamingText?: string | null;
		streamingStatus?: RunStatus | null;
		canDecide?: boolean;
		onDecide?: (item: ApprovalItem, decision: 'approve' | 'deny') => void;
	} = $props();

	let container: HTMLDivElement | undefined = $state();

	$effect(() => {
		// Snap to bottom whenever the list grows or streaming text updates.
		void items.length;
		void streamingText;
		void streamingStatus;
		if (container) container.scrollTop = container.scrollHeight;
	});

	let showStreamingBubble = $derived(streamingText !== null);
	// "Streaming" means deltas are still flowing — i.e. the run hasn't
	// reached a terminal status yet. Guards the O(n²) markdown render
	// during streaming (we show plain text instead) and the pulsing dot.
	let isActivelyStreaming = $derived(
		streamingStatus === null || streamingStatus === 'queued' || streamingStatus === 'running'
	);
</script>

<div bind:this={container} class="flex-1 overflow-y-auto px-6 py-4">
	<ul class="mx-auto flex max-w-3xl flex-col gap-4">
		{#each items as item (item.kind === 'message' ? item.id : item.kind === 'tool' ? `${item.run_id}:${item.call_id}` : `${item.run_id}:${item.approval_id}`)}
			<li>
				{#if item.kind === 'message'}
					<MessageBubble message={item} />
				{:else if item.kind === 'tool'}
					<ToolChip {item} live={item.run_id === liveRunId} />
				{:else if item.kind === 'approval'}
					<ApprovalCard
						{item}
						canDecide={canDecide && item.run_id === liveRunId}
						onDecide={(decision) => onDecide?.(item, decision)}
					/>
				{/if}
			</li>
		{/each}
		{#if showStreamingBubble}
			<li>
				<AssistantBubble text={streamingText ?? ''} streaming={isActivelyStreaming} />
				{#if streamingStatus === 'pending_approval'}
					<p class="mt-1 text-xs text-muted">Waiting for tool approval…</p>
				{/if}
			</li>
		{/if}
	</ul>
</div>
