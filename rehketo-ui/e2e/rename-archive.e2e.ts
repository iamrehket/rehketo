// Sidebar: the conversation actions menu dismisses cleanly, then rename → archive.
//
// Regression guard for the "phantom navigation" bug: the row's action menu
// used to live inside the conversation's <a href="/c/{id}"> link, so clicking
// Rename/Archive bubbled to the anchor and navigated to the conversation —
// discarding the action's effect. The toHaveURL assertions below fail if that
// ever returns: a menu click must never change the route. The Escape check
// guards the dismissal behavior (the menu no longer closes only on mouseleave).

import { test, expect, csrfHeaders } from './fixtures/auth';

test('menu dismisses on Escape, then rename then archive removes the conversation', async ({
	page,
	loggedInRequest,
	context
}) => {
	const created = await loggedInRequest.post('/conversations', {
		data: {},
		headers: await csrfHeaders(context)
	});
	expect(created.status()).toBe(201);

	await page.goto('/');

	const actions = page.getByLabel('Conversation actions').first();
	const rename = page.getByRole('button', { name: 'Rename' });

	// Open the menu, then dismiss it with Escape.
	await actions.hover();
	await actions.click();
	await expect(rename).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(rename).toHaveCount(0);

	// Reopen and rename. The menu click must not navigate away from the root.
	await actions.hover();
	await actions.click();
	await rename.click();
	await expect(page).not.toHaveURL(/\/c\//);

	const input = page.getByRole('textbox').first();
	await input.fill('renamed by e2e');
	await input.press('Enter');

	await expect(page.getByText('renamed by e2e')).toBeVisible();

	await actions.hover();
	await actions.click();
	await page.getByRole('button', { name: 'Archive' }).click();
	await expect(page).not.toHaveURL(/\/c\//);

	await expect(page.getByText('renamed by e2e')).toHaveCount(0);
});
