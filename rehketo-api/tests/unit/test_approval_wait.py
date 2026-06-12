"""wait_for_decisions consumes the run's event stream and returns once every
approval id in the batch has a decision. Unknown and duplicate approval ids
are ignored (the endpoint validates; the waiter just filters)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rehketo.agent.approval import wait_for_decisions


class FakeBus:
    """Replays scripted events, then blocks forever (like a live stream)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        raise AssertionError("waiter must not publish")

    async def subscribe(self, run_id: str, *, from_sequence: int | None = None):
        for e in self._events:
            yield e
        await asyncio.Event().wait()  # block: the waiter must return on its own


async def test_returns_when_all_decided() -> None:
    bus = FakeBus(
        [
            {"type": "message.delta", "delta": "x"},
            {
                "type": "tool.approval_decision",
                "approval_id": "a1",
                "decision": "approve",
            },
            {
                "type": "tool.approval_decision",
                "approval_id": "a2",
                "decision": "deny",
            },
        ]
    )
    decisions = await wait_for_decisions(bus, "r1", ["a1", "a2"])
    assert decisions == {"a1": "approve", "a2": "deny"}


async def test_ignores_unknown_and_duplicate_ids() -> None:
    bus = FakeBus(
        [
            {
                "type": "tool.approval_decision",
                "approval_id": "ghost",
                "decision": "deny",
            },
            {
                "type": "tool.approval_decision",
                "approval_id": "a1",
                "decision": "approve",
            },
            {
                "type": "tool.approval_decision",
                "approval_id": "a1",
                "decision": "deny",
            },
        ]
    )
    decisions = await wait_for_decisions(bus, "r1", ["a1"])
    assert decisions == {"a1": "approve"}  # first decision wins


async def test_blocks_until_decided() -> None:
    bus = FakeBus([])
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await wait_for_decisions(bus, "r1", ["a1"])


async def test_out_of_order_decisions_keyed_correctly() -> None:
    bus = FakeBus(
        [
            {"type": "tool.approval_decision", "approval_id": "a2", "decision": "deny"},
            {
                "type": "tool.approval_decision",
                "approval_id": "a1",
                "decision": "approve",
            },
        ]
    )
    decisions = await wait_for_decisions(bus, "r1", ["a1", "a2"])
    assert decisions == {"a1": "approve", "a2": "deny"}
