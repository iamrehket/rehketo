// Cancel a streaming reply mid-flight. Uses the fake Bifrost's `marathon`
// profile (40 chunks @ 250 ms, ~10 s total) so we have a deterministic window
// to click Cancel.
//
// DIAGNOSIS (2026-06-10): permission chain verified sound end-to-end —
// devonly login grants ['User','Admin'], both grant chat.cancel_run in
// rehketo-api/rehketo/permissions/roles.py, /me/capabilities feeds auth.can,
// and the root +layout.ts hydrates auth before any page renders. The original
// skip predated the durable-bus rewrite; the ~1 s window from the old `slow`
// profile (10 chunks @ 100 ms) was racing Playwright actionability before the
// streaming stack was replaced.

import { test, expect, assistantBubble, setBifrostProfile } from './fixtures/auth';

test('cancel mid-stream stops the reply', async ({ page, loggedInRequest }) => {
	test.setTimeout(60_000);
	await setBifrostProfile(loggedInRequest, 'marathon');

	await page.goto('/');
	await page.getByRole('button', { name: /new chat/i }).click();
	await expect(page).toHaveURL(/\/c\//);

	const composer = page.getByPlaceholder('Message Rehketo…');
	await composer.fill('please reply slowly');
	await page.getByRole('button', { name: 'Send' }).click();

	// Anchor: the stream is provably live before asserting the Cancel button.
	await expect(assistantBubble(page)).toContainText(/tok\d+/);

	const cancel = page.getByRole('button', { name: 'Cancel' });
	await expect(cancel).toBeVisible();
	await cancel.click();

	// Scoped to the bubble: an unscoped /cancelled/i could match elsewhere.
	// Badge.svelte renders the label as "Cancelled" (capital C).
	await expect(assistantBubble(page).getByText('Cancelled')).toBeVisible();
	await expect(page.getByPlaceholder('Message Rehketo…')).toBeEnabled();
});
