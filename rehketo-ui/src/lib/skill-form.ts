// Bodies for the doc-skill author surface (rehketo-api/rehketo/api/skills_me.py).
// name + kind are identity and never sent on PATCH. display_name is sent as
// null when blank so a user can clear it.
export type MySkillCreateBody = {
	name: string;
	display_name: string | null;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export type MySkillPatchBody = {
	display_name: string | null;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export type SkillFormState = {
	displayName: string;
	trigger: string;
	instructions: string;
	enabled: boolean;
};

export function buildSkillPatchBody(state: SkillFormState): MySkillPatchBody {
	return {
		display_name: state.displayName || null,
		trigger: state.trigger,
		instructions: state.instructions,
		enabled: state.enabled
	};
}

// Admin surface (rehketo-api/rehketo/api/skills_admin.py). One form authors
// both doc and mcp global skills, so the body carries only the backing field
// that matches `kind` (mirrors the DB skills_kind_backing check).
export type AdminSkillCreateBody = {
	name: string;
	display_name: string | null;
	kind: 'doc' | 'mcp';
	trigger: string;
	allowed_roles: string[];
	enabled: boolean;
	instructions?: string;
	mcp_server_id?: string;
};

export type AdminSkillPatchBody = {
	display_name: string | null;
	trigger: string;
	allowed_roles: string[];
	enabled: boolean;
	instructions?: string;
	mcp_server_id?: string;
};

export type AdminSkillFormState = {
	name: string;
	kind: 'doc' | 'mcp';
	displayName: string;
	trigger: string;
	instructions: string;
	mcpServerId: string;
	allowedRoles: string[];
	enabled: boolean;
};

export function buildAdminCreateBody(s: AdminSkillFormState): AdminSkillCreateBody {
	const body: AdminSkillCreateBody = {
		name: s.name,
		display_name: s.displayName || null,
		kind: s.kind,
		trigger: s.trigger,
		allowed_roles: s.allowedRoles,
		enabled: s.enabled
	};
	if (s.kind === 'doc') body.instructions = s.instructions;
	else body.mcp_server_id = s.mcpServerId;
	return body;
}

export function buildAdminPatchBody(s: AdminSkillFormState): AdminSkillPatchBody {
	// kind + name are identity (not patchable); send only the backing field
	// matching the existing kind.
	const body: AdminSkillPatchBody = {
		display_name: s.displayName || null,
		trigger: s.trigger,
		allowed_roles: s.allowedRoles,
		enabled: s.enabled
	};
	if (s.kind === 'doc') body.instructions = s.instructions;
	else body.mcp_server_id = s.mcpServerId;
	return body;
}
