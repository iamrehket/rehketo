// Pure grouping of transcript items into render groups: a run's thinking
// messages, tool calls, and approval cards collapse into one Working block;
// answers and user messages stay standalone bubbles. Pure function so the
// node test project covers it without DOM.

import type { ApprovalItem, MessageItem, ToolCallItem, TranscriptItem } from './types';

export type WorkingEntry =
	| { kind: 'text'; text: string }
	| { kind: 'tool'; item: ToolCallItem }
	| { kind: 'approval'; item: ApprovalItem };

export type RenderGroup =
	| { kind: 'bubble'; item: MessageItem }
	| { kind: 'working'; runId: string; entries: WorkingEntry[] };

export function groupTranscript(items: TranscriptItem[]): RenderGroup[] {
	const groups: RenderGroup[] = [];
	const workingFor = (runId: string): Extract<RenderGroup, { kind: 'working' }> => {
		const last = groups[groups.length - 1];
		if (last && last.kind === 'working' && last.runId === runId) return last;
		const next = { kind: 'working' as const, runId, entries: [] as WorkingEntry[] };
		groups.push(next);
		return next;
	};
	for (const item of items) {
		if (item.kind === 'tool') {
			workingFor(item.run_id).entries.push({ kind: 'tool', item });
		} else if (item.kind === 'approval') {
			workingFor(item.run_id).entries.push({ kind: 'approval', item });
		} else if (item.content.channel === 'thinking' && item.run_id !== null) {
			workingFor(item.run_id).entries.push({ kind: 'text', text: item.content.text });
		} else {
			groups.push({ kind: 'bubble', item });
		}
	}
	return groups;
}
