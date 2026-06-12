"""Segment tracking for streamed agent runs. A tool-using run emits multiple
AI messages (narration before each tool call, the answer after); each is a
segment, keyed by the message_id on its deltas. All segments but the last are
"thinking"; the last is the answer the UI renders as the reply bubble."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class Segment:
    message_id: str | None
    text: str = ""
    # Time of the segment's most recent delta. Persisted as the thinking
    # row's created_at: a turn's last token always arrives before the adapter
    # publishes the tool.call it triggered, so this timestamp sorts the
    # narration BEFORE its tool row in the transcript. (The boundary itself
    # is only detectable later — at the next segment's first delta — which
    # would sort after the tool rows.)
    last_delta_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SegmentTracker:
    def __init__(self) -> None:
        self._segments: list[Segment] = []

    def add_delta(self, message_id: str | None, delta: str) -> None:
        current = self._segments[-1] if self._segments else None
        if current is None or current.message_id != message_id:
            current = Segment(message_id=message_id)
            self._segments.append(current)
        current.text += delta
        current.last_delta_at = datetime.now(UTC)

    @property
    def thinking(self) -> list[Segment]:
        """Every segment but the last — narration that led to tool calls."""
        return self._segments[:-1]

    @property
    def answer_text(self) -> str:
        """The final segment's text; empty when the run produced none."""
        return self._segments[-1].text if self._segments else ""

    def thinking_or_current(self) -> list[Segment]:
        """All segments including the in-progress one (test/introspection)."""
        return list(self._segments)
