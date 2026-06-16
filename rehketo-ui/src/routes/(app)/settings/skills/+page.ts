import { error, redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { auth } from '$lib/stores/auth.svelte';
import { ApiError, type AdminSkillList, type McpServerList, type MySkillList } from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const mine = await apiFetch<MySkillList>('/me/skills', { skipAuthRedirect: true });
		if (auth.can('admin.manage_skills')) {
			const [allSkills, servers] = await Promise.all([
				apiFetch<AdminSkillList>('/admin/skills', { skipAuthRedirect: true }),
				apiFetch<McpServerList>('/admin/mcp-servers', { skipAuthRedirect: true })
			]);
			return { skills: mine.items, adminSkills: allSkills.items, servers: servers.items };
		}
		return { skills: mine.items, adminSkills: null, servers: null };
	} catch (err) {
		if (err instanceof ApiError) {
			if (err.status === 401) {
				const next = encodeURIComponent(url.pathname + url.search);
				throw redirect(302, `/login?next=${next}`);
			}
			throw error(err.status || 500, err.message);
		}
		throw err;
	}
};
