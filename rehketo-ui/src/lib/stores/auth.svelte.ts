// Rune-backed auth store. Hydrated once by +layout.ts via GET /me +
// GET /me/capabilities. Consumers read `authState` directly; components
// DO NOT fabricate capability bits — if /me/capabilities doesn't name
// an action, that capability is off.

import { SvelteSet } from 'svelte/reactivity';

import type { Capability, CapabilitiesOut, MeOut, User } from '$lib/types';

type AuthState = {
	user: User;
	capabilities: SvelteSet<Capability>;
};

let state = $state<AuthState | null>(null);

export const auth = {
	get current(): AuthState | null {
		return state;
	},
	get isAuthenticated(): boolean {
		return state !== null;
	},
	can(action: Capability): boolean {
		return state?.capabilities.has(action) ?? false;
	},
	hydrate(me: MeOut, caps: CapabilitiesOut): void {
		state = {
			user: { id: me.id, display_name: me.display_name, email: me.email },
			capabilities: new SvelteSet(caps.actions)
		};
	},
	clear(): void {
		state = null;
	}
};
