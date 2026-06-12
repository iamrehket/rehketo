import { describe, expect, it } from 'vitest';

import { groupTranscript } from './transcript';
import type { MessageItem, ToolCallItem, TranscriptItem } from './types';

function msg(overrides: Partial<MessageItem>): MessageItem {
	return {
		kind: 'message',
		id: 'm-1',
		conversation_id: 'c-1',
		role: 'assistant',
		content: { text: 'hi' },
		run_id: 'run-1',
		created_at: '2026-06-12T00:00:00Z',
		run_status: null,
		run_error: null,
		...overrides
	};
}

function tool(overrides: Partial<ToolCallItem> = {}): ToolCallItem {
	return {
		kind: 'tool',
		run_id: 'run-1',
		call_id: 'call-1',
		tool: 'testsrv__echo',
		arguments: { text: 'hi' },
		result: 'echo: hi',
		is_error: false,
		created_at: '2026-06-12T00:00:01Z',
		...overrides
	};
}

describe('groupTranscript', () => {
	it('keeps user and answer messages as standalone bubbles', () => {
		const items: TranscriptItem[] = [
			msg({ id: 'u-1', role: 'user', run_id: null }),
			msg({ id: 'a-1', content: { text: 'answer' } })
		];
		const groups = groupTranscript(items);
		expect(groups.map((g) => g.kind)).toEqual(['bubble', 'bubble']);
	});

	it("collapses a run's thinking, tool, and approval items into one working group", () => {
		const items: TranscriptItem[] = [
			msg({ id: 'u-1', role: 'user', run_id: null }),
			msg({ id: 't-1', content: { text: 'let me check', channel: 'thinking' } }),
			tool(),
			{
				kind: 'approval',
				run_id: 'run-1',
				approval_id: 'ap-1',
				tool: 'testsrv__echo',
				arguments: {},
				decision: 'approve',
				created_at: '2026-06-12T00:00:02Z'
			},
			msg({ id: 'a-1', content: { text: 'It is sunny.' } })
		];
		const groups = groupTranscript(items);
		expect(groups.map((g) => g.kind)).toEqual(['bubble', 'working', 'bubble']);
		const working = groups[1];
		if (working?.kind !== 'working') throw new Error('expected working group');
		expect(working.runId).toBe('run-1');
		expect(working.entries.map((e) => e.kind)).toEqual(['text', 'tool', 'approval']);
	});

	it('keeps working groups of different runs separate', () => {
		const items: TranscriptItem[] = [tool({ run_id: 'run-1' }), tool({ run_id: 'run-2' })];
		const groups = groupTranscript(items);
		expect(groups.map((g) => (g.kind === 'working' ? g.runId : ''))).toEqual(['run-1', 'run-2']);
	});

	it('tool-only runs still get a working group', () => {
		const groups = groupTranscript([tool(), msg({ id: 'a-1' })]);
		expect(groups.map((g) => g.kind)).toEqual(['working', 'bubble']);
	});
});
