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
