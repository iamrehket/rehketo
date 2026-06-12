from __future__ import annotations

from rehketo.agent.segments import SegmentTracker


def test_empty_tracker_has_empty_answer_and_no_thinking() -> None:
    t = SegmentTracker()
    assert t.answer_text == ""
    assert t.thinking == []


def test_single_message_id_is_one_segment_with_no_thinking() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "hel")
    t.add_delta("m1", "lo")
    assert t.answer_text == "hello"
    assert t.thinking == []


def test_message_id_change_moves_prior_segment_to_thinking() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "let me check")
    t.add_delta("m2", "It is sunny.")
    assert [s.text for s in t.thinking] == ["let me check"]
    assert t.thinking[0].message_id == "m1"
    assert t.answer_text == "It is sunny."


def test_three_turns_yield_two_thinking_segments_in_order() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "first")
    t.add_delta("m2", "second")
    t.add_delta("m3", "answer")
    assert [s.text for s in t.thinking] == ["first", "second"]
    assert t.answer_text == "answer"


def test_last_delta_at_advances_within_a_segment() -> None:
    t = SegmentTracker()
    t.add_delta("m1", "a")
    first = t.thinking_or_current()[-1].last_delta_at
    t.add_delta("m1", "b")
    assert t.thinking_or_current()[-1].last_delta_at >= first


def test_none_message_ids_group_into_one_segment() -> None:
    t = SegmentTracker()
    t.add_delta(None, "a")
    t.add_delta(None, "b")
    assert t.answer_text == "ab"
    assert t.thinking == []
