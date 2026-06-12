<script lang="ts">
	import ApprovalCard from './ApprovalCard.svelte';
	import ToolChip from './ToolChip.svelte';
	import type { WorkingEntry } from '$lib/transcript';
	import type { ApprovalItem } from '$lib/types';

	let {
		entries,
		live = false,
		canDecide = false,
		onDecide
	}: {
		entries: WorkingEntry[];
		live?: boolean;
		canDecide?: boolean;
		onDecide?: (item: ApprovalItem, decision: 'approve' | 'deny') => void;
	} = $props();

	// A pending approval keeps the block open even after the run pauses —
	// the decision buttons live inside it.
	let pendingApproval = $derived(
		entries.some((e) => e.kind === 'approval' && e.item.decision === null)
	);
	let label = $derived(`Working… (${entries.length} ${entries.length === 1 ? 'step' : 'steps'})`);

	// The user's manual toggle wins over auto-open — except a pending
	// approval, which pins the block open (the decision buttons are inside).
	let userToggled = $state<boolean | null>(null);
	let open = $derived((userToggled ?? live) || pendingApproval);

	function handleToggle(e: Event & { currentTarget: HTMLDetailsElement }): void {
		const el = e.currentTarget;
		const domOpen = el.open;
		// A pending approval pins the block open — the decision buttons are
		// inside and must remain reachable. Force the element back open
		// immediately and do not record a user toggle.
		if (pendingApproval && !domOpen) {
			el.open = true;
			return;
		}
		// Ignore toggle events that merely echo the auto-driven state;
		// a real user click is the one that disagrees with it.
		if (domOpen !== ((userToggled ?? live) || pendingApproval)) {
			userToggled = domOpen;
		}
	}
</script>

<details
	{open}
	ontoggle={handleToggle}
	data-working
	class="rounded-md border border-border bg-surface/40 text-xs text-muted"
>
	<summary class="cursor-pointer px-3 py-1.5">{label}</summary>
	<div class="space-y-2 border-t border-border px-3 py-2">
		{#each entries as entry, i (i)}
			{#if entry.kind === 'text'}
				<p class="whitespace-pre-wrap">{entry.text}</p>
			{:else if entry.kind === 'tool'}
				<ToolChip item={entry.item} {live} />
			{:else}
				<ApprovalCard
					item={entry.item}
					{canDecide}
					onDecide={(decision) => onDecide?.(entry.item, decision)}
				/>
			{/if}
		{/each}
	</div>
</details>
