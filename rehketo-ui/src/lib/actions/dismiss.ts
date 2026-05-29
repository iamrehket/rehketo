import type { Action } from 'svelte/action';

// Close a menu on outside pointerdown or Escape. Attach to the element that
// wraps the trigger AND the menu so clicking the trigger counts as "inside"
// (which also avoids the open-click re-toggle race). Listeners are attached
// only while `active`, so each closed menu — there's one per sidebar row —
// costs nothing.
export const dismiss: Action<HTMLElement, { active: boolean; onDismiss: () => void }> = (
	node,
	params
) => {
	let current = params;

	const onPointerDown = (e: PointerEvent) => {
		if (!node.contains(e.target as Node)) current.onDismiss();
	};
	const onKeydown = (e: KeyboardEvent) => {
		if (e.key === 'Escape') current.onDismiss();
	};
	const sync = (active: boolean) => {
		document.removeEventListener('pointerdown', onPointerDown, true);
		document.removeEventListener('keydown', onKeydown);
		if (active) {
			document.addEventListener('pointerdown', onPointerDown, true);
			document.addEventListener('keydown', onKeydown);
		}
	};

	sync(current.active);

	return {
		update(next) {
			current = next;
			sync(next.active);
		},
		destroy() {
			sync(false);
		}
	};
};
