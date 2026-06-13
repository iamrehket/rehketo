import { describe, expect, it } from 'vitest';

import { buildPatchBody, type McpFormState } from './mcp-server-form';

const base: McpFormState = {
	url: 'https://host/mcp',
	authToken: '',
	removeToken: false,
	allowedRoles: ['Admin'],
	autoApprove: false
};

describe('buildPatchBody', () => {
	it('always sends url, allowed_roles, auto_approve (never name or enabled)', () => {
		expect(buildPatchBody(base)).toEqual({
			url: 'https://host/mcp',
			allowed_roles: ['Admin'],
			auto_approve: false
		});
	});

	it('omits auth_token when blank and not removing (keep current)', () => {
		expect('auth_token' in buildPatchBody(base)).toBe(false);
	});

	it('forwards non-default url, allowed_roles, and auto_approve', () => {
		expect(
			buildPatchBody({
				...base,
				url: 'https://other/mcp',
				allowedRoles: ['Admin', 'Moderator', 'User'],
				autoApprove: true
			})
		).toMatchObject({
			url: 'https://other/mcp',
			allowed_roles: ['Admin', 'Moderator', 'User'],
			auto_approve: true
		});
	});

	it('sends the typed value when a token is entered (replace)', () => {
		expect(buildPatchBody({ ...base, authToken: 'secret' }).auth_token).toBe('secret');
	});

	it('sends null when remove is checked and field is blank (clear)', () => {
		expect(buildPatchBody({ ...base, removeToken: true }).auth_token).toBeNull();
	});

	it('lets a typed value win over the remove checkbox', () => {
		expect(buildPatchBody({ ...base, authToken: 'secret', removeToken: true }).auth_token).toBe(
			'secret'
		);
	});
});
