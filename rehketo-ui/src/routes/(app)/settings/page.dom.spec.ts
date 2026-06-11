// Settings page: renders the loaded instructions, gates Save on dirty/limit,
// and PUTs through apiFetch. $lib/api is mocked — no network.

import { flushSync, mount, unmount } from 'svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import SettingsPage from './+page.svelte';
import { apiFetch } from '$lib/api';
import { toasts } from '$lib/stores/toasts.svelte';
import { ApiError } from '$lib/types';

vi.mock('$lib/api', () => ({
	apiFetch: vi.fn(async () => ({ custom_instructions: 'updated' }))
}));

function mountPage(instructions: string) {
	return mount(SettingsPage, {
		target: document.body,
		// authenticated comes from the root layout's PageData.
		props: { data: { authenticated: true, preferences: { custom_instructions: instructions } } }
	});
}

function setTextarea(value: string): HTMLTextAreaElement {
	const textarea = document.querySelector('textarea');
	if (!textarea) throw new Error('textarea not rendered');
	textarea.value = value;
	textarea.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
	return textarea;
}

describe('settings page', () => {
	beforeEach(() => {
		document.body.innerHTML = '';
		vi.clearAllMocks();
	});

	it('renders the loaded instructions in the textarea', () => {
		const app = mountPage('be terse');
		const textarea = document.querySelector('textarea');
		expect(textarea?.value).toBe('be terse');
		unmount(app);
	});

	it('disables Save until the value changes', () => {
		const app = mountPage('be terse');
		const button = document.querySelector('button');
		expect(button?.disabled).toBe(true);
		setTextarea('be verbose');
		expect(button?.disabled).toBe(false);
		unmount(app);
	});

	it('disables Save when over the 4000-character limit', () => {
		const app = mountPage('');
		setTextarea('x'.repeat(4001));
		const button = document.querySelector('button');
		expect(button?.disabled).toBe(true);
		unmount(app);
	});

	it('PUTs the new value through apiFetch on Save', async () => {
		const app = mountPage('be terse');
		setTextarea('be verbose');
		document.querySelector('button')?.click();
		flushSync();
		expect(apiFetch).toHaveBeenCalledWith('/me/preferences', {
			method: 'PUT',
			body: JSON.stringify({ custom_instructions: 'be verbose' })
		});
		unmount(app);
	});

	it('pushes an error toast and retains the edited value when save fails', async () => {
		vi.mocked(apiFetch).mockRejectedValueOnce(new ApiError({ code: 'network', message: 'boom' }));
		const app = mountPage('be terse');
		setTextarea('be verbose');
		document.querySelector('button')?.click();
		flushSync();
		await vi.waitFor(() => {
			expect(toasts.items.some((t) => t.variant === 'error')).toBe(true);
		});
		expect(document.querySelector('textarea')?.value).toBe('be verbose');
		unmount(app);
	});

	it('disables Save again after a successful save', async () => {
		const app = mountPage('be terse');
		setTextarea('be verbose');
		document.querySelector('button')?.click();
		flushSync();
		await vi.waitFor(() => {
			expect(document.querySelector('button')?.disabled).toBe(true);
		});
		unmount(app);
	});
});
