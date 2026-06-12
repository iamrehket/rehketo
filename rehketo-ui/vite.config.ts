import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';
import { sveltekit } from '@sveltejs/kit/vite';

// Dev server origin must stay on :5173 so cookies set by /auth/callback
// land for the UI. Vite forwards API paths to the backend transparently.
const API_ORIGIN = 'http://127.0.0.1:8000';
const proxied = [
	'/admin',
	'/auth',
	'/conversations',
	'/runs',
	'/me',
	'/openapi.json',
	'/docs',
	'/healthz'
];

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	server: {
		proxy: Object.fromEntries(proxied.map((p) => [p, API_ORIGIN]))
	},
	test: {
		expect: { requireAssertions: true },
		projects: [
			{
				extends: './vite.config.ts',
				test: {
					name: 'server',
					environment: 'node',
					include: ['src/**/*.{test,spec}.{js,ts}'],
					exclude: ['src/**/*.svelte.{test,spec}.{js,ts}', 'src/**/*.dom.{test,spec}.{js,ts}']
				}
			},
			{
				extends: './vite.config.ts',
				// Without this, `import { mount } from 'svelte'` resolves to the
				// server runtime (exports.default), which refuses to mount.
				resolve: { conditions: ['browser'] },
				test: {
					name: 'dom',
					environment: 'jsdom',
					include: ['src/**/*.dom.{test,spec}.{js,ts}']
				}
			}
		]
	}
});
