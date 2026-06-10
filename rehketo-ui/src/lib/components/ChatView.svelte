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
		type ConversationDetail,
		type ErrorEnvelope,
		type MessageKickoffOut,
		type MessageOut,
		type RunStatus
	} from '$lib/types';

	let { conversation }: { conversation: ConversationDetail } = $props();

	// svelte-ignore state_referenced_locally
	let messages = $state<MessageOut[]>(conversation.messages);
	// svelte-ignore state_referenced_locally
	let title = $state(conversation.title);

	let streamingText = $state<string | null>(null);
	let streamingStatus = $state<RunStatus | null>(null);
	let streamingError = $state<ErrorEnvelope | null>(null);
	let activeRunId = $state<string | null>(null);
	let streamDisconnected = $state(false);

	let subscription: RunStreamSubscription | null = null;

	function resetStreaming(): void {
		streamingText = null;
		streamingStatus = null;
		streamingError = null;
		activeRunId = null;
	}

	function attachRun(runId: string): void {
		subscription?.unsubscribe();
		streamingText = '';
		streamingStatus = null;
		streamingError = null;
		activeRunId = runId;
		streamDisconnected = false;

		subscription = subscribeRun(runId, {
			onDelta: (delta) => {
				streamingText = (streamingText ?? '') + delta;
			},
			onMessageComplete: (message) => {
				messages = [...messages, message];
				streamingText = null;
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
			onEnded: () => {
				if (streamingStatus === 'failed' || streamingStatus === 'cancelled') {
					// Persist the partial bubble as a "terminal" assistant message
					// locally so reload semantics match live. The backend has also
					// persisted it with run_status set.
					if (streamingText !== null) {
						messages = [
							...messages,
							{
								id: `local-${activeRunId ?? ''}-terminal`,
								conversation_id: conversation.id,
								role: 'assistant',
								content: { text: streamingText },
								run_id: activeRunId,
								created_at: new Date(Date.now()).toISOString(),
								run_status: streamingStatus,
								run_error: streamingError
							}
						];
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
		messages = [
			...messages,
			{
				id: tempId,
				conversation_id: conversation.id,
				role: 'user',
				content: { text },
				run_id: null,
				created_at: now,
				run_status: null,
				run_error: null
			}
		];

		try {
			const kickoff = await apiFetch<MessageKickoffOut>(
				`/conversations/${conversation.id}/messages`,
				{
					method: 'POST',
					body: JSON.stringify({ content: text })
				}
			);
			// Reconcile the optimistic bubble with the server-assigned id.
			messages = messages.map((m) =>
				m.id === tempId ? { ...m, id: kickoff.message_id, run_id: kickoff.run_id } : m
			);
			conversations.bumpUpdatedAt(conversation.id);
			attachRun(kickoff.run_id);
		} catch (err) {
			// Roll back the optimistic bubble on failure.
			messages = messages.filter((m) => m.id !== tempId);
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

	<MessageList {messages} {streamingText} {streamingStatus} />

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
