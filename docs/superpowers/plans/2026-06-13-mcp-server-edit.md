# MCP Server Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin edit an existing MCP server's URL, auth token, roles, and auto-approve in place, instead of delete-and-recreate.

**Architecture:** UI-only change against the already-built `PATCH /admin/mcp-servers/{id}` endpoint. Extract the inline create-form markup into a reusable `McpServerForm.svelte` that works in create or edit mode; the parent page owns the POST-vs-PATCH decision. The token's keep/replace/clear intent lives in a pure helper so it can be tested in isolation.

**Tech Stack:** SvelteKit (Svelte 5 runes), TypeScript, Vitest (`.dom.spec.ts` via `mount`/`unmount`, plain `.spec.ts` for pure logic), Tailwind.

---

## File structure

- **Create** `rehketo-ui/src/lib/mcp-server-form.ts` — pure helper: maps form state to the PATCH body (the keep/replace/clear logic). One responsibility: payload construction.
- **Create** `rehketo-ui/src/lib/mcp-server-form.spec.ts` — unit tests for the helper.
- **Create** `rehketo-ui/src/lib/components/McpServerForm.svelte` — the create/edit form component (field markup + mode behavior).
- **Create** `rehketo-ui/src/lib/components/McpServerForm.dom.spec.ts` — DOM tests for the component.
- **Modify** `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte` — replace the inline Add form with the component, add the per-row Edit button + inline expand + `save()` handler.
- **Create** `rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts` — DOM test for the page's edit interaction.

Convention notes (verified against the codebase):
- Components use **callback props**, not event dispatchers — e.g. `ApprovalCard` takes `onDecide`. This form takes `onSubmit` / `onCancel`.
- `+page.svelte` snapshots its `data` into local `$state` with a `// svelte-ignore state_referenced_locally` comment. Same idiom applies inside the form for `server`.
- Tests select on `data-action="..."` / `data-*` hooks, not text/CSS.
- `McpServerOut` (`src/lib/types.ts:196`) exposes `has_auth_token: boolean` and never the token value.

---

## Task 1: Pure payload helper (`buildPatchBody`)

**Files:**
- Create: `rehketo-ui/src/lib/mcp-server-form.ts`
- Test: `rehketo-ui/src/lib/mcp-server-form.spec.ts`

- [ ] **Step 1: Write the failing test**

`rehketo-ui/src/lib/mcp-server-form.spec.ts`:

```ts
import { describe, expect, it } from 'vitest';

import { buildPatchBody, type McpFormState } from './mcp-server-form';

const base: McpFormState = {
	url: 'https://host/mcp',
	authToken: '',
	removeToken: false,
	allowedRoles: ['Admin'],
	autoApprove: false
};

describe('buildPatchBody', () => {
	it('always sends url, allowed_roles, auto_approve (never name or enabled)', () => {
		expect(buildPatchBody(base)).toEqual({
			url: 'https://host/mcp',
			allowed_roles: ['Admin'],
			auto_approve: false
		});
	});

	it('omits auth_token when blank and not removing (keep current)', () => {
		expect('auth_token' in buildPatchBody(base)).toBe(false);
	});

	it('sends the typed value when a token is entered (replace)', () => {
		expect(buildPatchBody({ ...base, authToken: 'secret' }).auth_token).toBe('secret');
	});

	it('sends null when remove is checked and field is blank (clear)', () => {
		expect(buildPatchBody({ ...base, removeToken: true }).auth_token).toBeNull();
	});

	it('lets a typed value win over the remove checkbox', () => {
		expect(buildPatchBody({ ...base, authToken: 'secret', removeToken: true }).auth_token).toBe(
			'secret'
		);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/mcp-server-form.spec.ts`
Expected: FAIL — `Failed to resolve import "./mcp-server-form"` / `buildPatchBody is not a function`.

- [ ] **Step 3: Write minimal implementation**

`rehketo-ui/src/lib/mcp-server-form.ts`:

```ts
// PATCH body for editing an MCP server. The token is write-only — the API
// returns only `has_auth_token`, so the edit field always opens blank. These
// three intents map to what the API distinguishes via `model_fields_set`
// (rehketo-api/rehketo/api/mcp_servers.py, McpServerPatch):
//   typed value         -> replace
//   blank + removeToken  -> clear (auth_token: null)
//   blank, no remove     -> keep  (auth_token omitted)
// `name` is immutable and `enabled` is owned by the row toggle, so the edit
// form sends neither.
export type McpServerPatchBody = {
	url: string;
	allowed_roles: string[];
	auto_approve: boolean;
	auth_token?: string | null;
};

export type McpFormState = {
	url: string;
	authToken: string;
	removeToken: boolean;
	allowedRoles: string[];
	autoApprove: boolean;
};

export function buildPatchBody(state: McpFormState): McpServerPatchBody {
	const body: McpServerPatchBody = {
		url: state.url,
		allowed_roles: state.allowedRoles,
		auto_approve: state.autoApprove
	};
	if (state.authToken) {
		body.auth_token = state.authToken; // replace — a typed value wins over remove
	} else if (state.removeToken) {
		body.auth_token = null; // clear
	}
	// else: omit auth_token -> keep current
	return body;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/mcp-server-form.spec.ts`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add rehketo-ui/src/lib/mcp-server-form.ts rehketo-ui/src/lib/mcp-server-form.spec.ts
git commit -m "feat: add MCP server edit PATCH-body helper"
```

---

## Task 2: `McpServerForm` component (create + edit modes)

**Files:**
- Create: `rehketo-ui/src/lib/components/McpServerForm.svelte`
- Test: `rehketo-ui/src/lib/components/McpServerForm.dom.spec.ts`

- [ ] **Step 1: Write the failing test**

`rehketo-ui/src/lib/components/McpServerForm.dom.spec.ts`:

```ts
import { mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';

import McpServerForm from './McpServerForm.svelte';
import type { McpServerOut } from '$lib/types';

function server(overrides: Partial<McpServerOut> = {}): McpServerOut {
	return {
		id: 'srv-1',
		name: 'github',
		url: 'https://host/mcp',
		has_auth_token: true,
		allowed_roles: ['Admin', 'Moderator'],
		enabled: true,
		auto_approve: false,
		created_at: '2026-06-13T00:00:00Z',
		updated_at: '2026-06-13T00:00:00Z',
		...overrides
	};
}

function teardown(app: Record<string, unknown>): void {
	unmount(app);
	document.body.innerHTML = '';
}

describe('McpServerForm', () => {
	it('create mode: editable name, no remove-token checkbox, submits a full create body', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: null, busy: false, onSubmit }
		});

		(document.querySelector('[data-field="name"]') as HTMLInputElement).value = 'github';
		(document.querySelector('[data-field="name"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		(document.querySelector('[data-field="url"]') as HTMLInputElement).value = 'https://h/mcp';
		(document.querySelector('[data-field="url"]') as HTMLInputElement).dispatchEvent(
			new Event('input', { bubbles: true })
		);
		expect(document.querySelector('[data-field="remove-token"]')).toBeNull();

		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		expect(onSubmit).toHaveBeenCalledTimes(1);
		expect(onSubmit.mock.calls[0][0]).toMatchObject({
			name: 'github',
			url: 'https://h/mcp',
			auth_token: null,
			enabled: true,
			auto_approve: false
		});
		teardown(app);
	});

	it('edit mode: name is read-only and prefilled', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server(), busy: false, onSubmit, onCancel: vi.fn() }
		});
		const nameEl = document.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameEl.readOnly).toBe(true);
		expect(nameEl.value).toBe('github');
		teardown(app);
	});

	it('edit mode with a token: shows remove checkbox; checking it sends auth_token null', () => {
		const onSubmit = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server({ has_auth_token: true }), busy: false, onSubmit, onCancel: vi.fn() }
		});
		const remove = document.querySelector('[data-field="remove-token"]') as HTMLInputElement;
		expect(remove).not.toBeNull();
		remove.click();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		expect(onSubmit.mock.calls[0][0]).toMatchObject({ auth_token: null });
		teardown(app);
	});

	it('edit mode without a token: no remove checkbox', () => {
		const app = mount(McpServerForm, {
			target: document.body,
			props: {
				server: server({ has_auth_token: false }),
				busy: false,
				onSubmit: vi.fn(),
				onCancel: vi.fn()
			}
		});
		expect(document.querySelector('[data-field="remove-token"]')).toBeNull();
		teardown(app);
	});

	it('edit mode: Cancel fires onCancel', () => {
		const onCancel = vi.fn();
		const app = mount(McpServerForm, {
			target: document.body,
			props: { server: server(), busy: false, onSubmit: vi.fn(), onCancel }
		});
		(document.querySelector('[data-action="cancel"]') as HTMLButtonElement).click();
		expect(onCancel).toHaveBeenCalledTimes(1);
		teardown(app);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/McpServerForm.dom.spec.ts`
Expected: FAIL — cannot resolve `./McpServerForm.svelte`.

- [ ] **Step 3: Write minimal implementation**

`rehketo-ui/src/lib/components/McpServerForm.svelte`:

```svelte
<script lang="ts">
	import { buildPatchBody, type McpServerPatchBody } from '$lib/mcp-server-form';
	import type { McpServerOut } from '$lib/types';

	// Source of truth for roles: rehketo-api/rehketo/permissions/roles.py.
	const ROLES = ['Admin', 'Moderator', 'User'];

	type CreateBody = {
		name: string;
		url: string;
		auth_token: string | null;
		allowed_roles: string[];
		enabled: boolean;
		auto_approve: boolean;
	};

	let {
		server = null,
		busy = false,
		onSubmit,
		onCancel
	}: {
		server?: McpServerOut | null;
		busy?: boolean;
		onSubmit: (body: CreateBody | McpServerPatchBody) => void;
		onCancel?: () => void;
	} = $props();

	const isEdit = server !== null;

	// `server` is a one-time initialiser: each form instance edits one row.
	// svelte-ignore state_referenced_locally
	let name = $state(server?.name ?? '');
	// svelte-ignore state_referenced_locally
	let url = $state(server?.url ?? '');
	let authToken = $state('');
	let removeToken = $state(false);
	// svelte-ignore state_referenced_locally
	let allowedRoles = $state<string[]>(server ? [...server.allowed_roles] : [...ROLES]);
	// svelte-ignore state_referenced_locally
	let autoApprove = $state(server?.auto_approve ?? false);

	function submit(): void {
		if (server) {
			onSubmit(buildPatchBody({ url, authToken, removeToken, allowedRoles, autoApprove }));
		} else {
			onSubmit({
				name,
				url,
				auth_token: authToken || null,
				allowed_roles: allowedRoles,
				enabled: true,
				auto_approve: autoApprove
			});
		}
	}

	const canSubmit = $derived(isEdit ? Boolean(url) : Boolean(name) && Boolean(url));
</script>

<div class="flex flex-col gap-3">
	<label class="text-xs text-muted" for="mcp-name">Name (tool prefix)</label>
	{#if isEdit}
		<input
			id="mcp-name"
			data-field="name"
			value={name}
			readonly
			class="rounded-md border border-border bg-surface p-2 text-sm text-muted"
		/>
	{:else}
		<input
			id="mcp-name"
			data-field="name"
			bind:value={name}
			placeholder="github"
			class="rounded-md border border-border bg-bg p-2 text-sm"
		/>
	{/if}

	<label class="text-xs text-muted" for="mcp-url">URL</label>
	<input
		id="mcp-url"
		data-field="url"
		bind:value={url}
		placeholder="https://host/mcp"
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>

	<label class="text-xs text-muted" for="mcp-token">
		Bearer token{isEdit ? '' : ' (optional, write-only)'}
	</label>
	<input
		id="mcp-token"
		data-field="token"
		bind:value={authToken}
		type="password"
		autocomplete="off"
		placeholder={isEdit && server?.has_auth_token ? 'leave blank to keep current token' : ''}
		class="rounded-md border border-border bg-bg p-2 text-sm"
	/>
	{#if isEdit && server?.has_auth_token}
		<label class="flex items-center gap-2 text-sm">
			<input
				data-field="remove-token"
				type="checkbox"
				bind:checked={removeToken}
				disabled={Boolean(authToken)}
			/>
			Remove existing token
		</label>
	{/if}

	<fieldset class="flex gap-4 text-sm">
		<legend class="text-xs text-muted">Allowed roles</legend>
		{#each ROLES as role (role)}
			<label class="flex items-center gap-1">
				<input type="checkbox" value={role} bind:group={allowedRoles} />
				{role}
			</label>
		{/each}
	</fieldset>

	<label class="flex items-center gap-2 text-sm">
		<input type="checkbox" bind:checked={autoApprove} />
		Auto-approve tool calls (trusted server — skips per-call user approval)
	</label>

	<div class="flex justify-end gap-2">
		{#if isEdit}
			<button
				type="button"
				data-action="cancel"
				onclick={() => onCancel?.()}
				class="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-surface-hover"
			>
				Cancel
			</button>
		{/if}
		<button
			type="button"
			data-action="submit"
			onclick={submit}
			disabled={busy || !canSubmit}
			class="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-50"
		>
			{isEdit ? 'Save' : 'Add'}
		</button>
	</div>
</div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/lib/components/McpServerForm.dom.spec.ts`
Expected: PASS — 5 passed.

- [ ] **Step 5: Commit**

```bash
git add rehketo-ui/src/lib/components/McpServerForm.svelte rehketo-ui/src/lib/components/McpServerForm.dom.spec.ts
git commit -m "feat: add reusable McpServerForm component"
```

---

## Task 3: Wire the form into the settings page (Edit + inline expand)

**Files:**
- Modify: `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte`
- Test: `rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts`

- [ ] **Step 1: Write the failing test**

`rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts`:

```ts
import { mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { McpServerOut } from '$lib/types';

const apiFetch = vi.fn();
vi.mock('$lib/api', () => ({ apiFetch: (...args: unknown[]) => apiFetch(...args) }));
vi.mock('$lib/stores/toasts.svelte', () => ({ toasts: { push: vi.fn() } }));

import Page from './+page.svelte';

function server(overrides: Partial<McpServerOut> = {}): McpServerOut {
	return {
		id: 'srv-1',
		name: 'github',
		url: 'https://host/mcp',
		has_auth_token: true,
		allowed_roles: ['Admin'],
		enabled: true,
		auto_approve: false,
		created_at: '2026-06-13T00:00:00Z',
		updated_at: '2026-06-13T00:00:00Z',
		...overrides
	};
}

function mountPage(servers: McpServerOut[]) {
	return mount(Page, { target: document.body, props: { data: { servers } } });
}

beforeEach(() => {
	apiFetch.mockReset();
});

afterEach(() => {
	document.body.innerHTML = '';
});

describe('MCP servers page — edit', () => {
	it('expands a row into an edit form when Edit is clicked', () => {
		const app = mountPage([server()]);
		expect(document.querySelector('[data-field="url"]')).toBeNull();
		(document.querySelector('[data-action="edit"]') as HTMLButtonElement).click();
		const nameEl = document.querySelector('[data-field="name"]') as HTMLInputElement;
		expect(nameEl.readOnly).toBe(true);
		expect(nameEl.value).toBe('github');
		unmount(app);
	});

	it('opens only one editor at a time', () => {
		const app = mountPage([server({ id: 'a', name: 'aaa' }), server({ id: 'b', name: 'bbb' })]);
		const editButtons = document.querySelectorAll('[data-action="edit"]');
		(editButtons[0] as HTMLButtonElement).click();
		(editButtons[1] as HTMLButtonElement).click();
		expect(document.querySelectorAll('[data-field="name"]').length).toBe(1);
		unmount(app);
	});

	it('PATCHes on save and updates the row', async () => {
		apiFetch.mockResolvedValue(server({ url: 'https://new/mcp' }));
		const app = mountPage([server()]);
		(document.querySelector('[data-action="edit"]') as HTMLButtonElement).click();
		(document.querySelector('[data-action="submit"]') as HTMLButtonElement).click();
		await Promise.resolve();
		expect(apiFetch).toHaveBeenCalledWith(
			'/admin/mcp-servers/srv-1',
			expect.objectContaining({ method: 'PATCH' })
		);
		unmount(app);
	});
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/routes/\(app\)/settings/mcp-servers/page.dom.spec.ts`
Expected: FAIL — no `[data-action="edit"]` button exists yet (querySelector returns null, `.click()` throws).

- [ ] **Step 3: Modify the page**

In `rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte`:

(a) Replace the `<script>` block's imports and state/handlers. Replace the top of the script (imports through the `create` function) with:

```ts
	import { apiFetch } from '$lib/api';
	import McpServerForm from '$lib/components/McpServerForm.svelte';
	import type { McpServerPatchBody } from '$lib/mcp-server-form';
	import { toasts } from '$lib/stores/toasts.svelte';
	import { ApiError, type McpServerOut } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	// Snapshot the server-loaded value into local state. data.servers is a one-time initialiser.
	// svelte-ignore state_referenced_locally
	let servers = $state<McpServerOut[]>(data.servers);
	let editingId = $state<string | null>(null);
	let busy = $state(false);

	type CreateBody = {
		name: string;
		url: string;
		auth_token: string | null;
		allowed_roles: string[];
		enabled: boolean;
		auto_approve: boolean;
	};

	function fail(action: string, err: unknown): void {
		if (err instanceof ApiError) console.warn(`${action} failed:`, err.code, err.message);
		// 403: apiFetch already fired the global forbidden hook; skip the
		// second toast to avoid duplicates (same pattern as settings page).
		if (!(err instanceof ApiError && err.status === 403)) {
			toasts.push({ variant: 'error', message: `Could not ${action} MCP server.` });
		}
	}

	async function create(body: CreateBody): Promise<void> {
		busy = true;
		try {
			const created = await apiFetch<McpServerOut>('/admin/mcp-servers', {
				method: 'POST',
				body: JSON.stringify(body)
			});
			servers = [created, ...servers];
			toasts.push({ variant: 'info', message: 'MCP server added.' });
		} catch (err) {
			fail('add', err);
		} finally {
			busy = false;
		}
	}

	async function save(server: McpServerOut, body: McpServerPatchBody): Promise<void> {
		busy = true;
		try {
			const updated = await apiFetch<McpServerOut>(`/admin/mcp-servers/${server.id}`, {
				method: 'PATCH',
				body: JSON.stringify(body)
			});
			servers = servers.map((s) => (s.id === updated.id ? updated : s));
			editingId = null;
			toasts.push({ variant: 'info', message: 'MCP server updated.' });
		} catch (err) {
			fail('update', err);
		} finally {
			busy = false;
		}
	}
```

Keep the existing `toggle`, `toggleAutoApprove`, and `remove` functions unchanged (they still live below `save`). The removed local field state (`name`, `url`, `authToken`, `allowedRoles`, `autoApprove`, the `ROLES` const) now lives in `McpServerForm`.

(b) In the row's button group, add an Edit button as the first button (before Disable):

```svelte
							<button
								type="button"
								data-action="edit"
								onclick={() => (editingId = editingId === server.id ? null : server.id)}
								class="rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-hover"
							>
								Edit
							</button>
```

(c) Inside each `<li>`, after the `<div class="flex items-center justify-between gap-3">...</div>` block, add the inline editor:

```svelte
					{#if editingId === server.id}
						<div class="mt-3 border-t border-border pt-3">
							<McpServerForm
								{server}
								{busy}
								onSubmit={(body) => save(server, body as McpServerPatchBody)}
								onCancel={() => (editingId = null)}
							/>
						</div>
					{/if}
```

(d) Replace the entire bottom `<section class="mt-8 ...">...</section>` (the old Add form, lines ~151-199) with:

```svelte
	<section class="mt-8 rounded-md border border-border bg-surface p-4">
		<h2 class="text-sm font-semibold">Add server</h2>
		<div class="mt-3">
			<McpServerForm server={null} {busy} onSubmit={(body) => create(body as CreateBody)} />
		</div>
	</section>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd rehketo-ui && pnpm run test:unit -- --run src/routes/\(app\)/settings/mcp-servers/page.dom.spec.ts`
Expected: PASS — 3 passed.

- [ ] **Step 5: Commit**

```bash
git add "rehketo-ui/src/routes/(app)/settings/mcp-servers/+page.svelte" "rehketo-ui/src/routes/(app)/settings/mcp-servers/page.dom.spec.ts"
git commit -m "feat: edit MCP servers inline from the settings page"
```

---

## Task 4: Full validation

**Files:** none (verification only).

- [ ] **Step 1: Lint and type-check the UI**

Run (from `rehketo-ui/`):

```bash
pnpm run lint
pnpm run check
```

Expected: lint clean; `svelte-check found 0 errors and 0 warnings`. If prettier flags formatting, run `pnpm run format` and re-run, then amend the relevant commit.

- [ ] **Step 2: Run the full UI unit suite**

Run (from `rehketo-ui/`):

```bash
pnpm run test:unit -- --run
```

Expected: all suites pass, including the three new files.

- [ ] **Step 3: Run the e2e flow (UI flow touched)**

Run (from `rehketo-api/`, postgres up via `just db`):

```bash
uv run pytest -m e2e
```

Expected: PASS. AGENTS.md flags this opt-in suite as prone to silent rot when UI flows change — this changes the MCP settings flow, so run it and quote the result. If it fails for a reason pre-dating this branch, fold the fix into this branch (per repo testing practice) rather than deferring.

- [ ] **Step 4: Final commit (only if Step 1 required a format pass not yet committed)**

```bash
git add -A
git commit -m "style: formatting for MCP server edit"
```

---

## Self-review (completed during planning)

- **Spec coverage:** inline expand → Task 3; shared `McpServerForm` create/edit → Tasks 2–3; token keep/replace/clear → Task 1 (helper) + Task 2 (UI wiring) + asserted in Tasks 2–3; name immutable → Task 2 (read-only) + helper omits it; error handling reuses `fail()` → Task 3; testing (unit + component + e2e) → Tasks 1, 2, 3, 4. No backend/schema/contract work — matches the UI-only scope.
- **Placeholder scan:** none — every code step has full code; every run step has an exact command and expected output.
- **Type consistency:** `McpServerPatchBody` / `McpFormState` / `buildPatchBody` (Task 1) are used verbatim in Tasks 2–3; `CreateBody` is defined identically in the form (Task 2) and the page (Task 3); `onSubmit` / `onCancel` callback-prop names match between component and page; `data-action` / `data-field` hooks match between components and their tests.
