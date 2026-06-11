import { redirect } from '@sveltejs/kit';

import { apiFetch } from '$lib/api';
import { ApiError, type PreferencesOut } from '$lib/types';
import type { PageLoad } from './$types';

export const ssr = false;
export const prerender = false;

export const load: PageLoad = async ({ url }) => {
	try {
		const preferences = await apiFetch<PreferencesOut>('/me/preferences', {
			skipAuthRedirect: true
		});
		return { preferences };
	} catch (err) {
		if (err instanceof ApiError && err.status === 401) {
			const next = encodeURIComponent(url.pathname + url.search);
			throw redirect(302, `/login?next=${next}`);
		}
		throw err;
	}
};
