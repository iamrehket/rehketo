// Durable-bus crash recovery: SIGKILL the api mid-stream; the worker survives
// and finishes the run, writing events to the durable run_events table. The
// restarted API reconnects to the completed run via the durable bus. The UI
// resumes the stream (durable bus + resume-by-sequence) and the conversation
// reloads cleanly showing the full assistant reply.
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

test('kill mid-stream → banner; worker finishes run; restart reconnects; reload shows reply', async ({
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

	// Capture the run id from the kickoff response — the worker's verdict is
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

	// /restart replies only after healthz is green — the cold langchain
	// re-import can take a while: widen the request timeout.
	const restart = await context.request.post(`${CHAOS_URL}/restart`, { timeout: 70_000 });
	expect(restart.ok(), 'chaos /restart failed').toBe(true);

	// Worker verdict: the worker finishes the run while the API is down and
	// writes the final status to run_events. The restarted API reads it from
	// the durable bus. The worker may still be draining right at restart, so
	// poll until 'succeeded' rather than asserting once.
	await expect
		.poll(
			async () => {
				const runResp = await context.request.get(`/runs/${runId}`);
				if (!runResp.ok()) return null;
				const run = (await runResp.json()) as { status: string };
				return run.status;
			},
			{ timeout: 20_000, intervals: [500, 1000, 2000] }
		)
		.toBe('succeeded');

	// Clean reload: the worker persisted the assistant message rows while the
	// API was down. The reloaded conversation shows the full reply and the
	// composer is enabled (active_run_id is null for completed runs).
	await page.goto(conversationUrl);
	await expect(userBubble(page)).toContainText('stream then crash');
	await expect(assistantBubble(page)).toContainText(/tok\d+/);
	await expect(page.getByPlaceholder('Message Rehketo…')).toBeEnabled();
});
