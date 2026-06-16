<script lang="ts">
	import { apiFetch } from '$lib/api';
	import SkillForm from '$lib/components/SkillForm.svelte';
	import type {
		AdminSkillCreateBody,
		AdminSkillPatchBody,
		MySkillCreateBody,
		MySkillPatchBody
	} from '$lib/skill-form';
	import { auth } from '$lib/stores/auth.svelte';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type AdminSkillOut, type McpServerOut, type MySkillOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();
	// svelte-ignore state_referenced_locally
	let skills = $state<MySkillOut[]>(data.skills);
	let editingId = $state<string | null>(null);
	let createBusy = $state(false);
	let editBusy = $state(false);

	let mine = $derived(skills.filter((s) => s.editable));

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} skill.` });
		}
	}

	async function create(body: MySkillCreateBody): Promise<void> {
		createBusy = true;
		try {
			const created = await apiFetch<MySkillOut>('/me/skills', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			skills = [created, ...skills];
			toasts.push({ variant: 'info', message: 'Skill added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			createBusy = false;
		}
	}

	async function save(skill: MySkillOut, body: MySkillPatchBody): Promise<void> {
		editBusy = true;
		try {
			const updated = await apiFetch<MySkillOut>(`/me/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			skills = skills.map((s) => (s.id === updated.id ? updated : s));
			editingId = null;
			toasts.push({ variant: 'info', message: 'Skill updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			editBusy = false;
		}
	}

	async function remove(skill: MySkillOut): Promise<void> {
		if (!confirm(`Delete skill "${skill.name}"?`)) return;
		try {
			await apiFetch(`/me/skills/${skill.id}`, { method: 'DELETE' });
			skills = skills.filter((s) => s.id !== skill.id);
		} catch (err) {
			fail('delete', err);
		}
	}

	// svelte-ignore state_referenced_locally
	let adminSkills = $state<AdminSkillOut[]>(data.adminSkills ?? []);
	// svelte-ignore state_referenced_locally
	const servers: McpServerOut[] = data.servers ?? [];
	let adminEditingId = $state<string | null>(null);
	let adminCreateBusy = $state(false);
	let adminEditBusy = $state(false);

	async function adminCreate(body: AdminSkillCreateBody): Promise<void> {
		adminCreateBusy = true;
		try {
			const created = await apiFetch<AdminSkillOut>('/admin/skills', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			adminSkills = [created, ...adminSkills];
			toasts.push({ variant: 'info', message: 'Skill created.' });
		} catch (err) {
			fail('create', err);
		} finally {
			adminCreateBusy = false;
		}
	}

	async function adminSave(skill: AdminSkillOut, body: AdminSkillPatchBody): Promise<void> {
		adminEditBusy = true;
		try {
			const updated = await apiFetch<AdminSkillOut>(`/admin/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			adminSkills = adminSkills.map((s) => (s.id === updated.id ? updated : s));
			adminEditingId = null;
			toasts.push({ variant: 'info', message: 'Skill updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			adminEditBusy = false;
		}
	}

	async function adminToggle(skill: AdminSkillOut): Promise<void> {
		try {
			const updated = await apiFetch<AdminSkillOut>(`/admin/skills/${skill.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ enabled: !skill.enabled })
			});
			adminSkills = adminSkills.map((s) => (s.id === updated.id ? updated : s));
		} catch (err) {
			fail('update', err);
		}
	}

	async function adminRemove(skill: AdminSkillOut): Promise<void> {
		if (!confirm(`Delete global skill "${skill.name}"?`)) return;
		try {
			await apiFetch(`/admin/skills/${skill.id}`, { method: 'DELETE' });
			adminSkills = adminSkills.filter((s) => s.id !== skill.id);
		} catch (err) {
			fail('delete', err);
		}
	}
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

	{#if auth.can('chat.author_skill')}
		<section class="mt-8">
			<h2 class="text-sm font-semibold">Your skills</h2>
			<ul class="mt-3 flex flex-col gap-3">
				{#each mine as skill (skill.id)}
					<li class="rounded-md border border-border bg-surface p-3">
						<div class="flex items-center justify-between gap-3">
							<span class="font-mono text-sm">{skill.name}</span>
							<div class="flex gap-2">
								<button
									type="button"
									data-action="edit"
									onclick={() => (editingId = editingId === skill.id ? null : skill.id)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									Edit
								</button>
								<button
									type="button"
									data-action="delete"
									onclick={() => remove(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
								>
									Delete
								</button>
							</div>
						</div>
						{#if editingId === skill.id}
							<div class="mt-3 border-t border-border pt-3">
								<SkillForm
									{skill}
									busy={editBusy}
									onSubmit={(body) => save(skill, body as MySkillPatchBody)}
									onCancel={() => (editingId = null)}
								/>
							</div>
						{/if}
					</li>
				{:else}
					<li class="text-sm text-muted">You haven't created any skills.</li>
				{/each}
			</ul>

			<div class="mt-4 rounded-md border border-border bg-surface p-4">
				<h3 class="text-sm font-semibold">New skill</h3>
				<div class="mt-3">
					<SkillForm
						skill={null}
						busy={createBusy}
						onSubmit={(body) => create(body as MySkillCreateBody)}
					/>
				</div>
			</div>
		</section>
	{/if}

	{#if auth.can('admin.manage_skills')}
		<section class="mt-8">
			<h2 class="text-sm font-semibold">Manage global / mcp skills</h2>
			<ul class="mt-3 flex flex-col gap-3">
				{#each adminSkills as skill (skill.id)}
					<li class="rounded-md border border-border bg-surface p-3">
						<div class="flex items-center justify-between gap-3">
							<div>
								<span class="font-mono text-sm">{skill.display_name ?? skill.name}</span>
								<span class="ml-2 rounded bg-bg px-1.5 py-0.5 text-xs text-muted">{skill.kind}</span
								>
								{#if !skill.enabled}<span class="ml-2 text-xs text-muted">disabled</span>{/if}
								<p class="text-xs text-muted">{skill.trigger}</p>
								{#if skill.allowed_roles.length}
									<p class="text-xs text-muted">roles: {skill.allowed_roles.join(', ')}</p>
								{/if}
							</div>
							<div class="flex gap-2">
								<button
									type="button"
									data-action="admin-edit"
									onclick={() => (adminEditingId = adminEditingId === skill.id ? null : skill.id)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									Edit
								</button>
								<button
									type="button"
									data-action="admin-toggle"
									onclick={() => adminToggle(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
								>
									{skill.enabled ? 'Disable' : 'Enable'}
								</button>
								<button
									type="button"
									data-action="admin-delete"
									onclick={() => adminRemove(skill)}
									class="rounded-md border border-border px-2 py-1 text-xs text-danger hover:bg-surface-hover"
								>
									Delete
								</button>
							</div>
						</div>
						{#if adminEditingId === skill.id}
							<div class="mt-3 border-t border-border pt-3">
								<SkillForm
									variant="admin"
									{skill}
									{servers}
									busy={adminEditBusy}
									onSubmit={(body) => adminSave(skill, body as AdminSkillPatchBody)}
									onCancel={() => (adminEditingId = null)}
								/>
							</div>
						{/if}
					</li>
				{:else}
					<li class="text-sm text-muted">No global skills configured.</li>
				{/each}
			</ul>

			<div class="mt-4 rounded-md border border-border bg-surface p-4">
				<h3 class="text-sm font-semibold">New global skill</h3>
				<div class="mt-3">
					<SkillForm
						variant="admin"
						skill={null}
						{servers}
						busy={adminCreateBusy}
						onSubmit={(body) => adminCreate(body as AdminSkillCreateBody)}
					/>
				</div>
			</div>
		</section>
	{/if}
</div>
