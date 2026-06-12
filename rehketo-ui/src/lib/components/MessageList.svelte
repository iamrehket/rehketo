<script lang="ts">
	import AssistantBubble from './AssistantBubble.svelte';
	import MessageBubble from './MessageBubble.svelte';
	import WorkingBlock from './WorkingBlock.svelte';
	import { groupTranscript } from '$lib/transcript';
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

	let groups = $derived(groupTranscript(items));
</script>

<div bind:this={container} class="flex-1 overflow-y-auto px-6 py-4">
	<ul class="mx-auto flex max-w-3xl flex-col gap-4">
		{#each groups as group, i (group.kind === 'bubble' ? group.item.id : `working:${group.runId}:${i}`)}
			<li>
				{#if group.kind === 'bubble'}
					<MessageBubble message={group.item} />
				{:else}
					<WorkingBlock
						entries={group.entries}
						live={group.runId === liveRunId}
						canDecide={canDecide && group.runId === liveRunId}
						{onDecide}
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
