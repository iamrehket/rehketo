import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';

import { subscribeRun, type RunStreamHandlers } from './sse';
import type { MessageOut, RunEvent } from './types';

// EventSource mock that matches browser dispatch semantics: SSE frames
// with `event: <name>` dispatch as custom events of that name — NOT as
// generic `message` events. The real backend emits `event: message.delta`,
// `event: run.ended`, etc., so tests must too, or we'd mask a class of
// bug where the client listens for the wrong event type.
class MockEventSource {
	static instances: MockEventSource[] = [];
	readonly url: string;
	closed = false;
	private listeners: Record<string, ((e: Event) => void)[]> = {};

	constructor(url: string) {
		this.url = url;
		MockEventSource.instances.push(this);
	}

	addEventListener(name: string, fn: (e: Event) => void): void {
		(this.listeners[name] ??= []).push(fn);
	}

	close(): void {
		this.closed = true;
	}

	emitEvent(event: RunEvent): void {
		const me = new MessageEvent(event.type, { data: JSON.stringify(event) });
		for (const fn of this.listeners[event.type] ?? []) fn(me);
	}

	emitRaw(eventType: string, data: string): void {
		const me = new MessageEvent(eventType, { data });
		for (const fn of this.listeners[eventType] ?? []) fn(me);
	}

	emitError(): void {
		for (const fn of this.listeners.error ?? []) fn(new Event('error'));
	}
}

function mkMessage(overrides: Partial<MessageOut> = {}): MessageOut {
	return {
		id: 'msg-1',
		conversation_id: 'conv-1',
		role: 'assistant',
		content: { text: 'hello' },
		run_id: 'run-1',
		created_at: '2026-04-21T00:00:00Z',
		run_status: 'succeeded',
		run_error: null,
		...overrides
	};
}

function collectHandlers(): {
	deltas: string[];
	completes: MessageOut[];
	statuses: string[];
	updates: { id: string; title: string }[];
	ended: number;
	errors: number;
	handlers: RunStreamHandlers;
} {
	const deltas: string[] = [];
	const completes: MessageOut[] = [];
	const statuses: string[] = [];
	const updates: { id: string; title: string }[] = [];
	let ended = 0;
	let errors = 0;
	return {
		deltas,
		completes,
		statuses,
		updates,
		ended,
		errors,
		get handlers(): RunStreamHandlers {
			return {
				onDelta: (d) => deltas.push(d),
				onMessageComplete: (m) => completes.push(m),
				onStatus: (s) => statuses.push(s),
				onConversationUpdated: (id, title) => updates.push({ id, title }),
				onEnded: () => {
					ended++;
				},
				onError: () => {
					errors++;
				}
			};
		}
	};
}

describe('subscribeRun', () => {
	beforeEach(() => {
		MockEventSource.instances.length = 0;
	});

	test('success flow: deltas → complete → succeeded → ended (run.ended closes)', () => {
		const c = collectHandlers();
		const sub = subscribeRun('run-1', c.handlers, {
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		const src = MockEventSource.instances[0]!;

		src.emitEvent({
			type: 'message.delta',
			delta: 'hel',
			message_id: 'm1',
			sequence: 1,
			run_id: 'run-1'
		});
		src.emitEvent({
			type: 'message.delta',
			delta: 'lo',
			message_id: 'm1',
			sequence: 2,
			run_id: 'run-1'
		});
		expect(sub.state).toBe('running');

		src.emitEvent({
			type: 'message.complete',
			message: mkMessage(),
			sequence: 3,
			run_id: 'run-1'
		});

		src.emitEvent({ type: 'run.status', status: 'succeeded', sequence: 4, run_id: 'run-1' });
		expect(sub.state).toBe('terminalSucceeded');
		expect(src.closed).toBe(false); // succeeded alone does NOT close

		src.emitEvent({
			type: 'conversation.updated',
			conversation_id: 'conv-1',
			title: 'new',
			sequence: 5,
			run_id: 'run-1'
		});

		src.emitEvent({ type: 'run.ended', sequence: 6, run_id: 'run-1' });
		expect(src.closed).toBe(true);
		expect(sub.state).toBe('terminalSucceeded');

		expect(c.deltas.join('')).toBe('hello');
		expect(c.completes).toHaveLength(1);
		expect(c.statuses).toEqual(['succeeded']);
		expect(c.updates).toEqual([{ id: 'conv-1', title: 'new' }]);
	});

	test('failure flow: delta → failed → ended (no message.complete)', () => {
		const c = collectHandlers();
		const sub = subscribeRun('run-f', c.handlers, {
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		const src = MockEventSource.instances[0]!;

		src.emitEvent({
			type: 'message.delta',
			delta: 'partial',
			message_id: 'm1',
			sequence: 1,
			run_id: 'run-f'
		});
		src.emitEvent({
			type: 'run.status',
			status: 'failed',
			error: { code: 'llm_failure', message: 'boom' },
			sequence: 2,
			run_id: 'run-f'
		});
		expect(sub.state).toBe('terminalFailed');
		expect(src.closed).toBe(false);

		src.emitEvent({ type: 'run.ended', sequence: 3, run_id: 'run-f' });
		expect(src.closed).toBe(true);
		expect(sub.state).toBe('terminalFailed');
		expect(c.completes).toEqual([]);
	});

	test('cancel flow: delta → cancelled → ended', () => {
		const c = collectHandlers();
		const sub = subscribeRun('run-c', c.handlers, {
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		const src = MockEventSource.instances[0]!;

		src.emitEvent({
			type: 'message.delta',
			delta: 'half',
			message_id: 'm1',
			sequence: 1,
			run_id: 'run-c'
		});
		src.emitEvent({ type: 'run.status', status: 'cancelled', sequence: 2, run_id: 'run-c' });
		src.emitEvent({ type: 'run.ended', sequence: 3, run_id: 'run-c' });

		expect(sub.state).toBe('terminalCancelled');
		expect(src.closed).toBe(true);
	});

	test('error after terminal status is treated as normal EOF — no onError', () => {
		// Browsers fire `error` whenever the server closes the HTTP stream,
		// including the normal EOF after run.ended. When we've already seen
		// a terminal run.status, suppress the disconnect signal.
		const onError = vi.fn();
		const onEnded = vi.fn();
		subscribeRun(
			'run-tail',
			{ onError, onEnded },
			{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
		);
		const src = MockEventSource.instances[0]!;

		src.emitEvent({
			type: 'run.status',
			status: 'failed',
			error: { code: 'llm_failure', message: 'overloaded' },
			sequence: 1,
			run_id: 'run-tail'
		});
		// Server closes the connection without sending run.ended first, OR
		// the native error fires racing against our run.ended handler.
		src.emitError();

		expect(onError).not.toHaveBeenCalled();
		expect(onEnded).toHaveBeenCalledTimes(1);
	});

	test('unsubscribe closes the underlying EventSource', () => {
		const c = collectHandlers();
		const sub = subscribeRun('run-u', c.handlers, {
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		sub.unsubscribe();
		expect(MockEventSource.instances[0]!.closed).toBe(true);
	});

	test('from_sequence query param is set when provided', () => {
		const c = collectHandlers();
		subscribeRun('run-r', c.handlers, {
			fromSequence: 7,
			EventSourceImpl: MockEventSource as unknown as typeof EventSource
		});
		expect(MockEventSource.instances[0]!.url).toBe('/runs/run-r/events?from_sequence=7');
	});

	describe('reconnect with resume', () => {
		beforeEach(() => {
			vi.useFakeTimers();
		});

		afterEach(() => {
			vi.useRealTimers();
		});

		function mkDelta(sequence: number, runId: string): RunEvent {
			return { type: 'message.delta', delta: 'x', message_id: 'm1', sequence, run_id: runId };
		}

		test('reconnects after a mid-run error with from_sequence = last seen + 1', () => {
			const onError = vi.fn();
			subscribeRun(
				'run-rc',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);
			const first = MockEventSource.instances[0]!;

			first.emitEvent(mkDelta(0, 'run-rc'));
			first.emitEvent(mkDelta(1, 'run-rc'));
			first.emitError();

			expect(MockEventSource.instances).toHaveLength(1);
			vi.advanceTimersByTime(500);
			expect(MockEventSource.instances).toHaveLength(2);
			expect(MockEventSource.instances[1]!.url).toContain('from_sequence=2');
			expect(onError).not.toHaveBeenCalled();
		});

		test('reconnect before any event was seen resumes from sequence 0', () => {
			const onError = vi.fn();
			subscribeRun(
				'run-zero',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);

			MockEventSource.instances[0]!.emitError();
			vi.advanceTimersByTime(500);

			expect(MockEventSource.instances).toHaveLength(2);
			expect(MockEventSource.instances[1]!.url).toContain('from_sequence=0');
			expect(onError).not.toHaveBeenCalled();
		});

		test('error from a superseded source is ignored', () => {
			// Polyfills/mocks may dispatch a queued error after close(); it
			// must not kill the healthy replacement or fork the retry chain.
			const onError = vi.fn();
			subscribeRun(
				'run-stale',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);
			const first = MockEventSource.instances[0]!;

			first.emitError();
			vi.advanceTimersByTime(500);
			expect(MockEventSource.instances).toHaveLength(2);

			first.emitError(); // stale: instance 1 is now the live source
			vi.advanceTimersByTime(60_000);

			expect(MockEventSource.instances).toHaveLength(2);
			expect(MockEventSource.instances[1]!.closed).toBe(false);
			expect(onError).not.toHaveBeenCalled();
		});

		test('malformed frame mid-stream joins the retry path, not onError', () => {
			const onError = vi.fn();
			const deltas: string[] = [];
			subscribeRun(
				'run-bad-frame',
				{ onError, onDelta: (d) => deltas.push(d) },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);
			const first = MockEventSource.instances[0]!;

			first.emitEvent(mkDelta(3, 'run-bad-frame'));
			first.emitRaw('message.delta', 'not-json');

			// Transport fault, not a stream verdict: the durable bus re-serves
			// the unparsed frame on resume.
			expect(onError).not.toHaveBeenCalled();
			expect(first.closed).toBe(true);
			vi.advanceTimersByTime(500);
			expect(MockEventSource.instances).toHaveLength(2);
			expect(MockEventSource.instances[1]!.url).toContain('from_sequence=4');

			MockEventSource.instances[1]!.emitEvent(mkDelta(4, 'run-bad-frame'));
			expect(deltas).toEqual(['x', 'x']);
			expect(onError).not.toHaveBeenCalled();
		});

		test('does not reconnect after run.ended', () => {
			const onError = vi.fn();
			subscribeRun(
				'run-ended',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);
			const src = MockEventSource.instances[0]!;

			src.emitEvent({ type: 'run.ended', sequence: 1, run_id: 'run-ended' });
			src.emitError();
			vi.advanceTimersByTime(10_000);

			expect(MockEventSource.instances).toHaveLength(1);
			expect(onError).not.toHaveBeenCalled();
		});

		test('does not reconnect after a terminal run.status', () => {
			const onError = vi.fn();
			const onEnded = vi.fn();
			subscribeRun(
				'run-term',
				{ onError, onEnded },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);
			const src = MockEventSource.instances[0]!;

			src.emitEvent({ type: 'run.status', status: 'succeeded', sequence: 1, run_id: 'run-term' });
			src.emitError();
			vi.advanceTimersByTime(10_000);

			// EOF-tail behavior preserved: close quietly, no retry, no error.
			expect(MockEventSource.instances).toHaveLength(1);
			expect(onEnded).toHaveBeenCalledTimes(1);
			expect(onError).not.toHaveBeenCalled();
		});

		test('surfaces onError after exhausting reconnect attempts', () => {
			const onError = vi.fn();
			const sub = subscribeRun(
				'run-exhaust',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);

			MockEventSource.instances[0]!.emitError();
			vi.advanceTimersByTime(500);
			MockEventSource.instances[1]!.emitError();
			vi.advanceTimersByTime(1000);
			MockEventSource.instances[2]!.emitError();
			vi.advanceTimersByTime(2000);
			expect(MockEventSource.instances).toHaveLength(4);
			expect(onError).not.toHaveBeenCalled();

			MockEventSource.instances[3]!.emitError();

			expect(MockEventSource.instances).toHaveLength(4);
			expect(onError).toHaveBeenCalledTimes(1);
			expect(sub.state).toBe('closed');
		});

		test('received events reset the attempt budget', () => {
			const onError = vi.fn();
			subscribeRun(
				'run-budget',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);

			MockEventSource.instances[0]!.emitError(); // attempt 1 of original budget
			vi.advanceTimersByTime(500);
			const second = MockEventSource.instances[1]!;
			second.emitEvent(mkDelta(5, 'run-budget')); // resets the budget
			second.emitError();

			vi.advanceTimersByTime(500); // back to the base delay after reset
			expect(MockEventSource.instances).toHaveLength(3);
			expect(MockEventSource.instances[2]!.url).toContain('from_sequence=6');

			// Full budget restarted: two more retries remain before exhaustion.
			MockEventSource.instances[2]!.emitError();
			vi.advanceTimersByTime(1000);
			MockEventSource.instances[3]!.emitError();
			vi.advanceTimersByTime(2000);
			expect(MockEventSource.instances).toHaveLength(5);
			expect(onError).not.toHaveBeenCalled();

			MockEventSource.instances[4]!.emitError(); // 4th post-reset error exhausts
			expect(onError).toHaveBeenCalledTimes(1);
		});

		test('unsubscribe during backoff cancels the pending reconnect', () => {
			const onError = vi.fn();
			const sub = subscribeRun(
				'run-cancel',
				{ onError },
				{ EventSourceImpl: MockEventSource as unknown as typeof EventSource }
			);

			MockEventSource.instances[0]!.emitError();
			sub.unsubscribe();
			vi.advanceTimersByTime(10_000);

			expect(MockEventSource.instances).toHaveLength(1);
			expect(onError).not.toHaveBeenCalled();
		});
	});
});
