<script lang="ts">
	import type { MySkillOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	// svelte-ignore state_referenced_locally
	let skills = $state<MySkillOut[]>(data.skills);
</script>

<div class="mx-auto w-full max-w-2xl overflow-y-auto px-6 py-8">
	<h1 class="text-lg font-semibold">Skills</h1>
	<p class="mt-1 text-sm text-muted">
		Capabilities the assistant can discover and use on your behalf.
	</p>

	<section class="mt-6">
		<h2 class="text-sm font-semibold">Skills available to you</h2>
		<ul class="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
			{#each skills as skill (skill.id)}
				<li class="rounded-md border border-border bg-surface p-3">
					<div class="flex items-center justify-between gap-2">
						<span class="font-mono text-sm">{skill.display_name ?? skill.name}</span>
						<span class="rounded bg-bg px-1.5 py-0.5 text-xs text-muted">{skill.kind}</span>
					</div>
					<p class="mt-1 text-xs text-muted">{skill.trigger}</p>
					<p class="mt-2 text-xs text-muted">
						{skill.source === 'owned' ? 'your skill' : 'global · read-only'}
					</p>
				</li>
			{:else}
				<li class="text-sm text-muted">No skills available yet.</li>
			{/each}
		</ul>
	</section>
</div>
