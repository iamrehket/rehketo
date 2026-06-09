// Sidebar account menu: the signed-in user's identity renders, and the menu
// dismisses on Escape / outside-click.
//
// The identity assertion is a regression guard for the /me contract: the
// backend returns a flat { id, display_name, email, roles }, and the UI must
// read those fields. A previously-nested MeOut type silently left the avatar
// empty, so the account menu never appeared.

import { test, expect } from './fixtures/auth';

const EMAIL = process.env.REHKETO_DEV_EMAIL ?? 'pw@example.com';

test('account menu shows the user and dismisses on Escape / outside click', async ({
	page,
	loggedInRequest
}) => {
	void loggedInRequest; // fixture establishes the session cookie before load
	await page.goto('/');

	// /me hydrated the user into the sidebar.
	await expect(page.getByText(EMAIL)).toBeVisible();

	const account = page.getByRole('button', { name: EMAIL });
	const logout = page.getByRole('button', { name: 'Log out' });

	// Escape closes.
	await account.click();
	await expect(logout).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(logout).toHaveCount(0);

	// Outside click closes.
	await account.click();
	await expect(logout).toBeVisible();
	await page.mouse.click(5, 5);
	await expect(logout).toHaveCount(0);
});
