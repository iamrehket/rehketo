<script lang="ts">
	import type { ApprovalItem } from '$lib/types';

	let {
		item,
		canDecide = false,
		onDecide
	}: {
		item: ApprovalItem;
		canDecide?: boolean;
		onDecide?: (decision: 'approve' | 'deny') => void;
	} = $props();
</script>

<div
	class="rounded-md border border-accent/40 bg-surface/60 text-xs"
	data-decision={item.decision ?? 'pending'}
>
	<div class="flex items-center gap-2 px-3 py-1.5 text-muted">
		{#if item.decision === null}
			<span
				class="h-2 w-2 animate-pulse rounded-full bg-accent"
				role="img"
				aria-label="awaiting approval"
			></span>
		{:else if item.decision === 'approve'}
			<span role="img" aria-label="approved">✓</span>
		{:else}
			<span class="text-danger" role="img" aria-label="denied">✗</span>
		{/if}
		<span class="font-mono">{item.tool}</span>
		<span>requests approval</span>
	</div>
	<pre class="overflow-x-auto whitespace-pre-wrap border-t border-border px-3 py-2">{JSON.stringify(
			item.arguments,
			null,
			2
		)}</pre>
	{#if item.decision === null && canDecide}
		<div class="flex gap-2 border-t border-border px-3 py-2">
			<button
				type="button"
				data-action="approve"
				onclick={() => onDecide?.('approve')}
				class="rounded-md bg-accent px-2 py-1 text-xs font-semibold text-white"
			>
				Approve
			</button>
			<button
				type="button"
				data-action="deny"
				onclick={() => onDecide?.('deny')}
				class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
			>
				Deny
			</button>
		</div>
	{/if}
</div>
