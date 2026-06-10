// Run event stream consumer.
//
// Protocol (spec §5.4):
// - success: message.delta* → message.complete → run.status=succeeded
//            → [conversation.updated] → run.ended
// - failure: message.delta* → run.status=failed → run.ended
// - cancel:  message.delta* → run.status=cancelled → run.ended
//
// The backend emits SSE frames with an `event:` field set to the event
// type (e.g. `event: message.delta`). Browsers dispatch those as custom
// DOM events of that name — NOT as generic `message` events — so we must
// addEventListener for each type. A single `addEventListener('message', …)`
// would miss every frame.
//
// The stream closes on `run.ended` only. run.status alone is a state
// signal, not a terminator — closing on succeeded would drop the
// subsequent conversation.updated.
//
// The bus is durable and `from_sequence` is inclusive, so a mid-run
// connection error triggers auto-reconnect resuming at the highest
// sequence seen + 1 (no duplicates, no gaps). Backoff is exponential
// (500ms/1s/2s, max 3 attempts); any received event refills the budget.
// onError fires only after the budget is exhausted. onError (exhausted
// budget) is followed by onEnded from the same close — consumers must
// not read onEnded alone as clean completion. A malformed frame
// is a transport fault on the same retry path (resume re-fetches it).
// Errors after a terminal run.status remain the normal EOF tail —
// close quietly, never reconnect.

import type { MessageOut, RunEvent, RunStatus } from './types';

export type StreamState =
	| 'idle'
	| 'queued'
	| 'running'
	| 'terminalSucceeded'
	| 'terminalFailed'
	| 'terminalCancelled'
	| 'closed';

export type RunStreamHandlers = {
	onDelta?: (delta: string, event: Extract<RunEvent, { type: 'message.delta' }>) => void;
	onMessageComplete?: (message: MessageOut) => void;
	onStatus?: (status: RunStatus, error: { code: string; message: string } | undefined) => void;
	onConversationUpdated?: (conversationId: string, title: string) => void;
	onEnded?: () => void;
	onError?: (err: unknown) => void;
};

export type RunStreamSubscription = {
	/** Reactive getter — the chat view can read this as `$derived` or
	 *  `$state` wrapper to reflect state in UI without wiring every
	 *  transition by hand. */
	readonly state: StreamState;
	unsubscribe: () => void;
};

// Factory takes an EventSource constructor so tests can inject a mock.
type EventSourceCtor = new (url: string, init?: EventSourceInit) => EventSource;

type TerminalState = 'terminalSucceeded' | 'terminalFailed' | 'terminalCancelled';

function isTerminal(state: StreamState): state is TerminalState {
	return (
		state === 'terminalSucceeded' || state === 'terminalFailed' || state === 'terminalCancelled'
	);
}

const MAX_RECONNECT_ATTEMPTS = 3;
const BASE_RETRY_MS = 500;

export function subscribeRun(
	runId: string,
	handlers: RunStreamHandlers,
	opts: { fromSequence?: number; EventSourceImpl?: EventSourceCtor } = {}
): RunStreamSubscription {
	const Ctor = opts.EventSourceImpl ?? (globalThis.EventSource as EventSourceCtor);

	const sub: { state: StreamState } = { state: 'idle' };
	let closed = false;
	let source: EventSource | null = null;
	let retryTimer: ReturnType<typeof setTimeout> | null = null;
	// Highest sequence seen; reconnects resume at last + 1 (from_sequence is
	// inclusive on the server). Starts one below the caller's fromSequence so
	// a pre-first-event reconnect re-requests the same window.
	let lastSequence = opts.fromSequence !== undefined ? opts.fromSequence - 1 : -1;
	let attempts = 0;

	function buildUrl(fromSequence?: number): string {
		const params = new URLSearchParams();
		if (fromSequence !== undefined && fromSequence >= 0) {
			params.set('from_sequence', String(fromSequence));
		}
		const qs = params.toString();
		return `/runs/${encodeURIComponent(runId)}/events${qs ? `?${qs}` : ''}`;
	}

	function close(final: StreamState): void {
		if (closed) return;
		closed = true;
		sub.state = final;
		if (retryTimer !== null) {
			clearTimeout(retryTimer);
			retryTimer = null;
		}
		source?.close();
		handlers.onEnded?.();
	}

	// Mid-run fault: the bus is durable, so close the current source and
	// resume from the next sequence instead of surfacing a disconnect —
	// until the attempt budget runs out.
	function retryOrFail(err: unknown): void {
		source?.close();
		if (attempts < MAX_RECONNECT_ATTEMPTS) {
			const delay = BASE_RETRY_MS * 2 ** attempts;
			attempts += 1;
			retryTimer = setTimeout(() => {
				retryTimer = null;
				if (closed) return;
				connect(lastSequence + 1);
			}, delay);
			return;
		}
		handlers.onError?.(err);
		close('closed');
	}

	function parseOrError<E extends RunEvent>(evt: Event): E | null {
		const data = (evt as MessageEvent<string>).data;
		try {
			return JSON.parse(data) as E;
		} catch (err) {
			// A frame we can't parse is a transport fault, not a stream
			// verdict: the frame never advanced lastSequence, so the durable
			// bus re-serves it on resume.
			retryOrFail(err);
			return null;
		}
	}

	// Every delivered event proves the connection works: record progress and
	// refill the reconnect budget.
	function track(event: { sequence: number }): void {
		lastSequence = event.sequence;
		attempts = 0;
	}

	function connect(fromSequence?: number): void {
		const self = new Ctor(buildUrl(fromSequence), { withCredentials: true });
		source = self;

		self.addEventListener('message.delta', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'message.delta' }>>(evt);
			if (!event) return;
			track(event);
			if (sub.state === 'idle' || sub.state === 'queued') sub.state = 'running';
			handlers.onDelta?.(event.delta, event);
		});

		self.addEventListener('message.complete', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'message.complete' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onMessageComplete?.(event.message);
		});

		self.addEventListener('conversation.updated', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'conversation.updated' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onConversationUpdated?.(event.conversation_id, event.title);
		});

		self.addEventListener('run.status', (evt) => {
			const event = parseOrError<Extract<RunEvent, { type: 'run.status' }>>(evt);
			if (!event) return;
			track(event);
			handlers.onStatus?.(event.status, event.error);
			if (event.status === 'queued') {
				if (sub.state === 'idle') sub.state = 'queued';
			} else if (event.status === 'running') {
				sub.state = 'running';
			} else if (event.status === 'succeeded') {
				sub.state = 'terminalSucceeded';
			} else if (event.status === 'failed') {
				sub.state = 'terminalFailed';
			} else if (event.status === 'cancelled') {
				sub.state = 'terminalCancelled';
			}
		});

		self.addEventListener('run.ended', () => {
			close(isTerminal(sub.state) ? sub.state : 'closed');
		});

		self.addEventListener('error', (err) => {
			// Ignore events from a superseded source: the HTML spec says a
			// closed EventSource dispatches nothing further, but polyfills
			// and mocks don't all honor that.
			if (closed || source !== self) return;
			// Normal EOF tail after a terminal status — close quietly (the
			// server closes the HTTP stream after run.ended).
			if (isTerminal(sub.state)) {
				close(sub.state);
				return;
			}
			retryOrFail(err);
		});
	}

	connect(opts.fromSequence);

	return {
		get state(): StreamState {
			return sub.state;
		},
		unsubscribe(): void {
			if (closed) return;
			closed = true;
			if (retryTimer !== null) clearTimeout(retryTimer);
			source?.close();
		}
	};
}
