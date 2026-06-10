// Shared Playwright fixtures + helpers for the offline e2e suite.
//
// `test` is Playwright's `base` extended with a `loggedInRequest` fixture
// that POSTs /auth/devonly/login (the API endpoint gated by
// DEVONLY_LOGIN_ENABLED=true that the pytest fixture sets). The login
// response sets session + CSRF cookies onto the browser `context`, so
// subsequent `page.goto('/')` calls land on the authenticated SPA shell
// rather than being redirected to /login.
//
// IMPORTANT: Playwright's `context.request` does NOT automatically add the
// X-CSRF-Token header that the SPA's `apiFetch` adds. For unsafe methods
// (POST/PUT/PATCH/DELETE) tests must use `csrfHeaders(context)` to read
// the rehketo_csrf cookie and include it as the header.

import {
	test as base,
	expect,
	type APIRequestContext,
	type BrowserContext,
	type Locator,
	type Page
} from '@playwright/test';

type AuthFixtures = {
	loggedInRequest: APIRequestContext;
};

export const test = base.extend<AuthFixtures>({
	loggedInRequest: async ({ context }, use) => {
		const email = process.env.REHKETO_DEV_EMAIL ?? 'pw@example.com';
		const resp = await context.request.post('/auth/devonly/login', {
			data: { email, display_name: 'Playwright', roles: ['User', 'Admin'] }
		});
		expect(resp.status(), `devonly login failed: ${await resp.text()}`).toBe(200);
		await use(context.request);
	}
});

// The bifrost profile is process-global on the fake server. Resetting to
// 'default' after every spec prevents a marathon-profile leak from blowing
// a later spec's time budget.
test.afterEach(async ({ request }) => {
	await setBifrostProfile(request, 'default');
});

export { expect };

/** Read the rehketo_csrf cookie and return it as the X-CSRF-Token header. */
export async function csrfHeaders(context: BrowserContext): Promise<Record<string, string>> {
	const cookies = await context.cookies();
	const csrf = cookies.find((c) => c.name === 'rehketo_csrf')?.value;
	if (!csrf) throw new Error('rehketo_csrf cookie not set — did login complete?');
	return { 'X-CSRF-Token': csrf };
}

/** Switch the fake Bifrost into a named profile (default | slow | title-fail | marathon). */
export async function setBifrostProfile(
	request: APIRequestContext,
	profile: 'default' | 'slow' | 'title-fail' | 'marathon'
): Promise<void> {
	const bifrostUrl = process.env.REHKETO_BIFROST_URL;
	if (!bifrostUrl) throw new Error('REHKETO_BIFROST_URL not set');
	const adminUrl = bifrostUrl.replace(/\/v1\/?$/, '') + '/__test__/mode';
	const resp = await request.post(adminUrl, { data: { profile } });
	expect(resp.status(), `set bifrost profile=${profile} failed`).toBe(200);
}

/** Scope selectors to the assistant message bubbles (AssistantBubble.svelte's outer `flex justify-start`). */
export function assistantBubble(page: Page): Locator {
	return page.locator('div.justify-start');
}

/** Scope selectors to the user message bubbles (UserBubble.svelte's outer `flex justify-end`). */
export function userBubble(page: Page): Locator {
	return page.locator('div.justify-end');
}
