import { describe, expect, it } from 'vitest';

import {
	buildAdminCreateBody,
	buildAdminPatchBody,
	buildSkillPatchBody,
	type AdminSkillFormState,
	type SkillFormState
} from './skill-form';

const base: SkillFormState = {
	displayName: '',
	trigger: 'use when X',
	instructions: 'do Y',
	enabled: true
};

describe('buildSkillPatchBody', () => {
	it('sends trigger, instructions, enabled, and display_name (null when blank)', () => {
		expect(buildSkillPatchBody(base)).toEqual({
			display_name: null,
			trigger: 'use when X',
			instructions: 'do Y',
			enabled: true
		});
	});

	it('forwards a non-blank display_name', () => {
		expect(buildSkillPatchBody({ ...base, displayName: 'My Notes' }).display_name).toBe('My Notes');
	});
});

const adminBase: AdminSkillFormState = {
	name: 'policy',
	kind: 'doc',
	displayName: '',
	trigger: 'reimburse',
	instructions: 'Steps.',
	mcpServerId: '',
	allowedRoles: ['User'],
	enabled: true
};

describe('buildAdminCreateBody', () => {
	it('sends instructions and omits mcp_server_id for a doc skill', () => {
		const body = buildAdminCreateBody(adminBase);
		expect(body).toMatchObject({ name: 'policy', kind: 'doc', instructions: 'Steps.' });
		expect('mcp_server_id' in body).toBe(false);
	});

	it('sends mcp_server_id and omits instructions for an mcp skill', () => {
		const body = buildAdminCreateBody({
			...adminBase,
			kind: 'mcp',
			instructions: '',
			mcpServerId: 'srv-1'
		});
		expect(body).toMatchObject({ kind: 'mcp', mcp_server_id: 'srv-1' });
		expect('instructions' in body).toBe(false);
	});
});

describe('buildAdminPatchBody', () => {
	it('never sends name or kind, and sends only the matching backing field', () => {
		const body = buildAdminPatchBody({ ...adminBase, kind: 'mcp', mcpServerId: 'srv-2' });
		expect('name' in body).toBe(false);
		expect('kind' in body).toBe(false);
		expect(body.mcp_server_id).toBe('srv-2');
		expect('instructions' in body).toBe(false);
	});
});
