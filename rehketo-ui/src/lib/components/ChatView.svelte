<script lang="ts">
	import { onDestroy } from 'svelte';

	import ChatHeader from './ChatHeader.svelte';
	import Composer from './Composer.svelte';
	import MessageList from './MessageList.svelte';
	import { apiFetch } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';
	import { conversations } from '$lib/stores/conversations.svelte';
	import { subscribeRun, type RunStreamSubscription } from '$lib/sse';
	import {
		ApiError,
		type ApprovalItem,
		type ConversationDetail,
		type ErrorEnvelope,
		type MessageKickoffOut,
		type MessageOut,
		type RunStatus,
		type TranscriptItem
	} from '$lib/types';

	let { conversation }: { conversation: ConversationDetail } = $props();

	// svelte-ignore state_referenced_locally
	let items = $state<TranscriptItem[]>(conversation.items);
	// svelte-ignore state_referenced_locally
	let title = $state(conversation.title);

	let streamingText = $state<string | null>(null);
	let streamingMessageId = $state<string | null>(null);
	// Monotonic suffix for local thinking ids — replaced by persisted rows
	// when message.complete arrives.
	let localThinkingSeq = 0;
	let streamingStatus = $state<RunStatus | null>(null);
	let streamingError = $state<ErrorEnvelope | null>(null);
	let activeRunId = $state<string | null>(null);
	let streamDisconnected = $state(false);

	let subscription: RunStreamSubscription | null = null;

	function resetStreaming(): void {
		streamingText = null;
		streamingMessageId = null;
		streamingStatus = null;
		streamingError = null;
		activeRunId = null;
	}

	// The current streaming segment is proven to be narration (not the
	// answer) the moment the model calls a tool, asks for approval, or
	// starts a new message. Fold it into a local thinking item so it
	// renders inside the working block, above the activity it led to.
	function foldStreamingTail(): void {
		const text = streamingText;
		streamingMessageId = null;
		if (text === null || text.length === 0 || activeRunId === null) return;
		const folded: MessageOut = {
			id: `local-thinking-${activeRunId}-${localThinkingSeq++}`,
			conversation_id: conversation.id,
			role: 'assistant',
			content: { text, channel: 'thinking' },
			run_id: activeRunId,
			created_at: new Date(Date.now()).toISOString(),
			run_status: null,
			run_error: null
		};
		items = [...items, { ...folded, kind: 'message' as const }];
		streamingText = '';
	}

	function attachRun(runId: string): void {
		subscription?.unsubscribe();
		streamingText = '';
		streamingStatus = null;
		streamingError = null;
		activeRunId = runId;
		streamDisconnected = false;

		subscription = subscribeRun(runId, {
			onDelta: (delta, event) => {
				if (streamingMessageId !== null && streamingMessageId !== event.message_id) {
					foldStreamingTail();
				}
				streamingMessageId = event.message_id;
				streamingText = (streamingText ?? '') + delta;
			},
			onMessageComplete: (message) => {
				// Persisted rows replace the local thinking items synthesized
				// during streaming — same text, server-authoritative ids.
				items = items.filter(
					(i) => !(i.kind === 'message' && i.id.startsWith(`local-thinking-${message.run_id}`))
				);
				// Replay can deliver a message.complete the conversation GET
				// already included — dedupe by id rather than trust ordering.
				if (!items.some((i) => i.kind === 'message' && i.id === message.id)) {
					items = [...items, { ...message, kind: 'message' as const }];
				}
				// Thinking rows arrive first; only the answer's complete (no
				// channel marker) ends the streaming bubble.
				if (message.content.channel !== 'thinking') {
					streamingText = null;
					streamingMessageId = null;
				}
			},
			onStatus: (status, error) => {
				streamingStatus = status;
				streamingError = error ?? null;
			},
			onConversationUpdated: (conversationId, newTitle) => {
				if (conversationId === conversation.id) {
					title = newTitle;
					conversations.patchTitle(conversationId, newTitle);
				}
			},
			onToolCall: (event) => {
				foldStreamingTail();
				// Replay can re-deliver a call already present from the GET.
				if (
					!items.some(
						(i) => i.kind === 'tool' && i.run_id === event.run_id && i.call_id === event.call_id
					)
				) {
					items = [
						...items,
						{
							kind: 'tool',
							run_id: event.run_id,
							call_id: event.call_id,
							tool: event.tool,
							arguments: event.arguments,
							result: null,
							is_error: null,
							created_at: new Date(Date.now()).toISOString()
						}
					];
				}
			},
			onToolResult: (event) => {
				items = items.map((i) =>
					i.kind === 'tool' && i.run_id === event.run_id && i.call_id === event.call_id
						? { ...i, result: event.result, is_error: event.is_error }
						: i
				);
			},
			onApprovalRequired: (event) => {
				foldStreamingTail();
				if (
					!items.some(
						(i) =>
							i.kind === 'approval' &&
							i.run_id === event.run_id &&
							i.approval_id === event.approval_id
					)
				) {
					items = [
						...items,
						{
							kind: 'approval',
							run_id: event.run_id,
							approval_id: event.approval_id,
							tool: event.tool,
							arguments: event.arguments,
							decision: null,
							created_at: new Date(Date.now()).toISOString()
						}
					];
				}
			},
			onApprovalDecision: (event) => {
				// Resolve on the EVENT, not the POST response — a second tab
				// (or another device) resolves the same card this way.
				items = items.map((i) =>
					i.kind === 'approval' && i.approval_id === event.approval_id
						? { ...i, decision: event.decision }
						: i
				);
			},
			onEnded: () => {
				if (streamingStatus === 'failed' || streamingStatus === 'cancelled') {
					// Persist the partial bubble as a "terminal" assistant message
					// locally so reload semantics match live. The backend has also
					// persisted it with run_status set.
					if (streamingText !== null) {
						const terminalMessage: MessageOut = {
							id: `local-${activeRunId ?? ''}-terminal`,
							conversation_id: conversation.id,
							role: 'assistant',
							content: { text: streamingText },
							run_id: activeRunId,
							created_at: new Date(Date.now()).toISOString(),
							run_status: streamingStatus,
							run_error: streamingError
						};
						items = [...items, { ...terminalMessage, kind: 'message' as const }];
					}
				}
				resetStreaming();
			},
			onError: () => {
				streamDisconnected = true;
				resetStreaming();
			}
		});
	}

	// Reattach to an in-flight run on open: replay from sequence 0 rebuilds
	// the streaming bubble, then live events continue. This is what makes the
	// durable bus visible — start a run on one device, watch it on another.
	// svelte-ignore state_referenced_locally
	if (conversation.active_run_id) {
		attachRun(conversation.active_run_id);
	}

	async function handleSend(text: string): Promise<void> {
		// Optimistic user bubble (will be replaced with server's id once the
		// POST resolves — matching ids keep reload semantics correct).
		const tempId = `local-${Date.now()}`;
		const now = new Date(Date.now()).toISOString();
		const optimisticMessage: MessageOut = {
			id: tempId,
			conversation_id: conversation.id,
			role: 'user',
			content: { text },
			run_id: null,
			created_at: now,
			run_status: null,
			run_error: null
		};
		items = [...items, { ...optimisticMessage, kind: 'message' as const }];

		try {
			const kickoff = await apiFetch<MessageKickoffOut>(
				`/conversations/${conversation.id}/messages`,
				{
					method: 'POST',
					body: JSON.stringify({ content: text })
				}
			);
			// Reconcile the optimistic bubble with the server-assigned id.
			items = items.map((i) =>
				i.kind === 'message' && i.id === tempId
					? { ...i, id: kickoff.message_id, run_id: kickoff.run_id }
					: i
			);
			conversations.bumpUpdatedAt(conversation.id);
			attachRun(kickoff.run_id);
		} catch (err) {
			// Roll back the optimistic bubble on failure.
			items = items.filter((i) => !(i.kind === 'message' && i.id === tempId));
			if (err instanceof ApiError) console.warn('send failed:', err.code, err.message);
		}
	}

	let isStreaming = $derived(activeRunId !== null);

	async function cancelActiveRun(): Promise<void> {
		const runId = activeRunId;
		if (!runId) return;
		try {
			await apiFetch(`/runs/${runId}/cancel`, { method: 'POST' });
		} catch (err) {
			// 409 = run already terminal (it finished between click and POST).
			// The SSE stream already dispatched the terminal event, so no UI
			// action is needed — just swallow.
			if (err instanceof ApiError && err.status === 409) return;
			if (err instanceof ApiError) console.warn('cancel failed:', err.code, err.message);
		}
	}

	async function decideApproval(item: ApprovalItem, decision: 'approve' | 'deny'): Promise<void> {
		try {
			await apiFetch(`/runs/${item.run_id}/approvals/${item.approval_id}`, {
				method: 'POST',
				body: JSON.stringify({ decision })
			});
		} catch (err) {
			// 409 = already decided (other tab) or run no longer pending; the
			// decision event (or terminal status) updates the card — swallow.
			if (err instanceof ApiError && err.status === 409) return;
			if (err instanceof ApiError) console.warn('approval failed:', err.code, err.message);
		}
	}

	onDestroy(() => {
		subscription?.unsubscribe();
		subscription = null;
	});
</script>

<div class="flex h-full flex-col">
	<ChatHeader conversationId={conversation.id} {title} />

	{#if streamDisconnected}
		<div class="border-b border-danger/40 bg-danger/10 px-6 py-2 text-sm text-danger" role="alert">
			Disconnected — reload to resume.
		</div>
	{/if}

	<MessageList
		{items}
		liveRunId={activeRunId}
		{streamingText}
		{streamingStatus}
		canDecide={auth.can('chat.approve_tool_call')}
		onDecide={decideApproval}
	/>

	{#if isStreaming && auth.can('chat.cancel_run')}
		<div class="flex justify-center border-t border-border bg-bg/80 px-6 py-2">
			<button
				type="button"
				onclick={cancelActiveRun}
				class="rounded-md border border-border bg-surface px-3 py-1 text-xs text-muted transition-colors hover:bg-surface-hover hover:text-danger"
			>
				Cancel
			</button>
		</div>
	{/if}

	<Composer {isStreaming} onSend={handleSend} />

	{#if !auth.can('chat.write')}
		<div class="border-t border-border bg-bg/80 px-6 py-3 text-sm text-muted">
			You don't have permission to send messages in this workspace.
		</div>
	{/if}
</div>
