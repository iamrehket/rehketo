<script lang="ts">
	import type { ToolCallItem } from '$lib/types';

	let { item, live = false }: { item: ToolCallItem; live?: boolean } = $props();

	let status = $derived(
		item.result === null && item.is_error === null
			? live
				? 'running'
				: 'incomplete'
			: item.is_error
				? 'error'
				: 'done'
	);
</script>

<details class="rounded-md border border-border bg-surface/60 text-xs" data-status={status}>
	<summary class="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-muted">
		{#if status === 'running'}
			<span class="h-2 w-2 animate-pulse rounded-full bg-accent" role="img" aria-label="running"
			></span>
		{:else if status === 'error'}
			<span class="text-danger" role="img" aria-label="failed">✗</span>
		{:else if status === 'incomplete'}
			<span role="img" aria-label="no result">—</span>
		{:else}
			<span role="img" aria-label="succeeded">✓</span>
		{/if}
		<span class="font-mono">{item.tool}</span>
	</summary>
	<div class="space-y-2 border-t border-border px-3 py-2">
		<pre class="overflow-x-auto whitespace-pre-wrap">{JSON.stringify(item.arguments, null, 2)}</pre>
		{#if item.result !== null}
			<pre class="overflow-x-auto whitespace-pre-wrap text-muted">{item.result}</pre>
		{/if}
	</div>
</details>
