// Durable-bus crash recovery: SIGKILL the api mid-stream (no lifespan
// shutdown — the run stays `running` in the DB), watch SSE retries exhaust
// into the disconnected banner, restart the api (startup sweep fails the
// orphaned run), and reload to a clean, usable conversation.
//
import { test, expect, assistantBubble, userBubble, setBifrostProfile } from './fixtures/auth';

// The main session suite (test_browser_flows.py) matches every *.e2e.ts
// against the UNKILLABLE in-thread session server; this spec only runs
// under the chaos pytest module, which is what sets REHKETO_CHAOS_URL.
test.skip(
	!process.env.REHKETO_CHAOS_URL,
	'requires the chaos-control fixture (test_restart_recovery.py)'
);

const CHAOS_URL = process.env.REHKETO_CHAOS_URL ?? '';

test('kill mid-stream → banner; restart → sweep fails run; reload is clean', async ({
	page,
	context,
	loggedInRequest
}) => {
	// Restart re-imports langchain mid-spec (5-15 s) on top of the ~10 s
	// marathon stream — the global 30 s budget is far too tight.
	test.setTimeout(90_000);

	await setBifrostProfile(loggedInRequest, 'marathon'); // 40 × "tok{i} " at 250 ms

	await page.goto('/');
	await expect(page.getByRole('button', { name: /new chat/i })).toBeVisible();

	await page.getByRole('button', { name: /new chat/i }).click();
	await expect(page).toHaveURL(/\/c\//);
	const conversationUrl = page.url();

	// Capture the run id from the kickoff response — the sweep verdict is
	// asserted via GET /runs/{id} after restart.
	const kickoffPromise = page.waitForResponse(
		(r) => r.url().includes('/messages') && r.request().method() === 'POST'
	);
	const composer = page.getByPlaceholder('Message Rehketo…');
	await composer.fill('stream then crash');
	await page.getByRole('button', { name: 'Send' }).click();
	const { run_id: runId } = (await (await kickoffPromise).json()) as { run_id: string };

	// Anchor: the stream is provably live before we pull the plug.
	await expect(assistantBubble(page)).toContainText(/tok\d+/);

	const kill = await context.request.post(`${CHAOS_URL}/kill`);
	expect(kill.ok(), 'chaos /kill failed').toBe(true);

	// SSE retries (500+1000+2000 ms) exhaust against instant ECONNREFUSED,
	// then the banner renders. Generous timeout for slow CI.
	await expect(page.getByRole('alert')).toContainText(/disconnected/i, { timeout: 20_000 });

	// /restart replies only after healthz is green — the startup sweep runs
	// in lifespan before the app serves, so it has completed by then. The
	// cold langchain re-import can take a while: widen the request timeout.
	const restart = await context.request.post(`${CHAOS_URL}/restart`, { timeout: 70_000 });
	expect(restart.ok(), 'chaos /restart failed').toBe(true);

	// Sweep verdict. context.request shares the browser's session cookie and
	// resolves relative URLs against baseURL (REHKETO_BASE_URL).
	const runResp = await context.request.get(`/runs/${runId}`);
	expect(runResp.ok(), `GET /runs/${runId} failed: ${await runResp.text()}`).toBe(true);
	const run = (await runResp.json()) as { status: string };
	expect(run.status).toBe('failed');

	// Clean reload: a SIGKILLed run leaves NO assistant message row — the
	// sweep only flips runs.status. active_run_id is null for failed runs,
	// so there's no reattach hang and the composer is live again.
	await page.goto(conversationUrl);
	await expect(userBubble(page)).toContainText('stream then crash');
	await expect(assistantBubble(page)).toHaveCount(0);
	await expect(page.getByPlaceholder('Message Rehketo…')).toBeEnabled();
});
