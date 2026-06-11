// Backend contract types — hand-maintained to match
// rehketo-api/docs/superpowers/specs/2026-04-20-svelte-ui-v1-design.md.
// Field names are snake_case to match the API wire format so the parsed
// JSON flows through without transformation.

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export type ErrorEnvelope = {
	code: string;
	message: string;
};

export type User = {
	id: string;
	display_name: string | null;
	email: string | null;
};

// The 11 canonical actions from rehketo-api/rehketo/permissions/actions.py.
// Keep in sync — if the backend adds one, this is the single source of
// truth on the UI side.
export type Capability =
	| 'chat.create_conversation'
	| 'chat.view_conversation'
	| 'chat.rename_conversation'
	| 'chat.delete_conversation'
	| 'chat.write'
	| 'chat.cancel_run'
	| 'chat.upload_files'
	| 'chat.use_mcp_server'
	| 'admin.manage_users'
	| 'admin.view_audit'
	| 'admin.manage_mcp_servers';

// The /me response is flat (matches rehketo-api/rehketo/api/me.py). `User` is
// assembled from these fields in auth.hydrate; the backend is the contract
// source of truth (see tools/check_contract.py).
export type MeOut = {
	id: string;
	display_name: string | null;
	email: string | null;
	roles: string[];
};

export type CapabilitiesOut = {
	actions: Capability[];
};

// Matches rehketo-api/rehketo/api/me.py PreferencesOut.
export type PreferencesOut = {
	custom_instructions: string;
};

export type ConversationSummary = {
	id: string;
	title: string | null;
	created_at: string;
	updated_at: string;
};

export type ConversationList = {
	items: ConversationSummary[];
};

export type MessageContent = {
	text: string;
};

export type MessageOut = {
	id: string;
	conversation_id: string;
	role: MessageRole;
	content: MessageContent;
	run_id: string | null;
	created_at: string;
	// Terminal run state joined from runs table. Null when the message has
	// no linked run (user messages) or the run is still in flight.
	run_status: RunStatus | null;
	run_error: ErrorEnvelope | null;
};

export type ConversationDetail = ConversationSummary & {
	messages: MessageOut[];
	active_run_id: string | null;
};

export type MessageKickoffOut = {
	message_id: string;
	run_id: string;
};

// SSE event union — matches rehketo/agent/events.py + rehketo/agent/run.py
// emissions. The stream closes on `run.ended`, NOT on `run.status`.
export type RunEvent =
	| {
			type: 'message.delta';
			delta: string;
			message_id: string;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'message.complete';
			message: MessageOut;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'conversation.updated';
			conversation_id: string;
			title: string;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'run.status';
			status: RunStatus;
			error?: ErrorEnvelope;
			sequence: number;
			run_id: string;
	  }
	| { type: 'run.ended'; sequence: number; run_id: string }
	| {
			type: 'tool.call';
			call_id: string;
			tool: string;
			arguments: Record<string, unknown>;
			sequence: number;
			run_id: string;
	  }
	| {
			type: 'tool.result';
			call_id: string;
			result: string;
			is_error: boolean;
			sequence: number;
			run_id: string;
	  };

// Transcript items — matches rehketo-api/rehketo/api/conversations.py
// MessageItem / ToolCallItem discriminated union.
export type MessageItem = MessageOut & { kind: 'message' };

export type ToolCallItem = {
	kind: 'tool';
	run_id: string;
	call_id: string;
	tool: string;
	arguments: Record<string, unknown>;
	result: string | null;
	is_error: boolean | null;
	created_at: string;
};

export type TranscriptItem = MessageItem | ToolCallItem;

// Matches rehketo-api/rehketo/api/mcp_servers.py McpServerOut.
export type McpServerOut = {
	id: string;
	name: string;
	url: string;
	has_auth_token: boolean;
	allowed_roles: string[];
	enabled: boolean;
	created_at: string;
	updated_at: string;
};

export type McpServerList = {
	items: McpServerOut[];
};

export class ApiError extends Error {
	readonly code: string;
	readonly status: number;

	constructor(opts: { code: string; message: string; status?: number }) {
		super(opts.message);
		this.name = 'ApiError';
		this.code = opts.code;
		this.status = opts.status ?? 0;
	}
}
