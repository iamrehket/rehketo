// Durable-bus regression: a run started in tab1 must be observable live in
// tab2 (GET /conversations/{id} → active_run_id → subscribe → replay-from-0),
// and both tabs must converge to ONE assistant bubble (message.complete
// dedupe-by-id). Same context = same session cookie = no second login.

import { test, expect, assistantBubble, setBifrostProfile } from './fixtures/auth';

// .trim() drops the trailing space on the last token deliberately — the server
// emits "tok39 " (with a trailing space) but we match with toContainText
// (substring), not toHaveText (exact), so trimming the constant is correct.
const FULL_TEXT = Array.from({ length: 40 }, (_, i) => `tok${i} `)
	.join('')
	.trim();

test('run started in tab1 streams live into tab2 and both converge', async ({
	page,
	context,
	loggedInRequest
}) => {
	// Marathon streams for ~10 s; the global 30 s budget is too tight once
	// tab2's cold SPA open and the 20 s convergence waits are added on CI.
	test.setTimeout(60_000);

	await setBifrostProfile(loggedInRequest, 'marathon'); // 40 × "tok{i} " at 250 ms

	await page.goto('/');
	await expect(page.getByRole('button', { name: /new chat/i })).toBeVisible();

	await page.getByRole('button', { name: /new chat/i }).click();
	await expect(page).toHaveURL(/\/c\//);
	const conversationUrl = page.url();

	const composer = page.getByPlaceholder('Message Rehketo…');
	await composer.fill('stream slowly please');
	await page.getByRole('button', { name: 'Send' }).click();

	// Anchor: the stream is provably live in tab1 before tab2 opens.
	await expect(assistantBubble(page)).toContainText(/tok\d+/);

	// Guard: stream provably still in flight before tab2 opens — otherwise
	// this spec silently degrades to testing cold-load of a finished chat.
	await expect(assistantBubble(page)).not.toContainText(FULL_TEXT);

	// Tab2 opens the same conversation mid-stream and sends NOTHING — deltas
	// arriving here prove the active_run_id → reattach → replay path.
	const page2 = await context.newPage();
	await page2.goto(conversationUrl);
	await expect(assistantBubble(page2)).toContainText(/tok\d+/);

	// Mid-stream proof: if tab2 had cold-loaded a completed conversation,
	// the bubble would already contain the full text.
	await expect(assistantBubble(page2)).not.toContainText(FULL_TEXT);

	// Both tabs converge to the full streamed text. Scoped to the assistant
	// bubble — the sidebar title also echoes the text after title-gen.
	await expect(assistantBubble(page)).toContainText(FULL_TEXT, { timeout: 20_000 });
	await expect(assistantBubble(page2)).toContainText(FULL_TEXT, { timeout: 20_000 });

	// Dedupe regression: a double-push into messages[] (the dedupe-by-id guard
	// in onMessageComplete) would produce two bubbles instead of one. Asserting
	// count=1 catches that specific failure; it does NOT test streaming-bubble /
	// message coexistence (those resolve atomically in one handler).
	await expect(assistantBubble(page)).toHaveCount(1);
	await expect(assistantBubble(page2)).toHaveCount(1);

	await page2.close();
});
