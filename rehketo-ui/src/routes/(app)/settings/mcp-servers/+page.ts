import { error, redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { ApiError, type McpServerList } from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const res = await apiFetch<McpServerList>('/admin/mcp-servers', {
			skipAuthRedirect: true
		});
		return { servers: res.items };
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
