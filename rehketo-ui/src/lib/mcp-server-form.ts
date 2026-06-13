// PATCH body for editing an MCP server. The token is write-only — the API
// returns only `has_auth_token`, so the edit field always opens blank. These
// three intents map to what the API distinguishes via `model_fields_set`
// (rehketo-api/rehketo/api/mcp_servers.py, McpServerPatch):
//   typed value         -> replace
//   blank + removeToken  -> clear (auth_token: null)
//   blank, no remove     -> keep  (auth_token omitted)
// `name` is immutable and `enabled` is owned by the row toggle, so the edit
// form sends neither.
export type McpServerPatchBody = {
	url: string;
	allowed_roles: string[];
	auto_approve: boolean;
	auth_token?: string | null;
};

// Full body for creating a server (POST /admin/mcp-servers). Unlike the PATCH
// body, `name` and `enabled` are always sent on create. Shared by the form
// component and the settings page so the shape can't drift between them.
export type McpServerCreateBody = {
	name: string;
	url: string;
	auth_token: string | null;
	allowed_roles: string[];
	enabled: boolean;
	auto_approve: boolean;
};

export type McpFormState = {
	url: string;
	authToken: string;
	removeToken: boolean;
	allowedRoles: string[];
	autoApprove: boolean;
};

export function buildPatchBody(state: McpFormState): McpServerPatchBody {
	const body: McpServerPatchBody = {
		url: state.url,
		allowed_roles: state.allowedRoles,
		auto_approve: state.autoApprove
	};
	if (state.authToken) {
		body.auth_token = state.authToken; // replace
	} else if (state.removeToken) {
		body.auth_token = null; // clear
	}
	// else: omit auth_token -> keep current
	return body;
}
