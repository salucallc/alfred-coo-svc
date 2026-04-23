"""AB-04 tests: state checkpoint/restore.

`OrchestratorState.to_json` + `from_json` round-trips. Soul memory read
path tolerates both list and {"memories": [...]} shapes.
"""

from __future__ import annotations

import json

from alfred_coo.autonomous_build.state import (
    OrchestratorState,
    checkpoint,
    restore,
    state_topic_for,
)


class _FakeSoul:
    """Minimal SoulClient double. Records writes + replays them on read.

    `recent_memories` returns in reverse-chronological order (newest first),
    matching the real soul-svc contract.
    """

    def __init__(self, initial: list[dict] | None = None):
        self.writes: list[dict] = []
        self.reads: list[dict] = list(initial or [])

    async def write_memory(self, content, topics=None):
        rec = {"content": content, "topics": topics or []}
        self.writes.append(rec)
        # Prepend so the next restore() sees this as the newest.
        self.reads.insert(0, rec)
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        # Filter by any matching topic (soul-svc semantics are "any").
        if topics:
            filtered = [
                m for m in self.reads
                if any(t in (m.get("topics") or []) for t in topics)
            ]
        else:
            filtered = list(self.reads)
        return filtered[:limit]


async def test_state_checkpoint_and_restore():
    """Write state, read back, assert field-level equality."""
    soul = _FakeSoul()
    kickoff = "kick-123"
    s = OrchestratorState(kickoff_task_id=kickoff)
    s.current_wave = 2
    s.cumulative_spend_usd = 4.20
    s.ticket_status = {"uuid-1": "merged_green", "uuid-2": "pending"}
    s.dispatched_child_tasks = {"uuid-1": "child-1"}
    s.ss08_acked = True
    s.record_event("test", note="hello")

    resp = await checkpoint(s, soul, kickoff)
    assert resp is not None
    assert len(soul.writes) == 1
    assert state_topic_for(kickoff) in soul.writes[0]["topics"]

    restored = await restore(soul, kickoff)
    assert restored is not None
    assert restored.kickoff_task_id == kickoff
    assert restored.current_wave == 2
    assert abs(restored.cumulative_spend_usd - 4.20) < 1e-9
    assert restored.ticket_status == {"uuid-1": "merged_green", "uuid-2": "pending"}
    assert restored.dispatched_child_tasks == {"uuid-1": "child-1"}
    assert restored.ss08_acked is True
    assert restored.events and restored.events[-1]["kind"] == "test"


async def test_restore_returns_none_when_no_prior_state():
    soul = _FakeSoul()
    restored = await restore(soul, "kick-unknown")
    assert restored is None


async def test_restore_ignores_mismatched_kickoff_id():
    """A memory entry with a different kickoff id must not be returned."""
    soul = _FakeSoul(
        initial=[
            {
                "content": json.dumps({
                    "kickoff_task_id": "other-kickoff",
                    "current_wave": 3,
                }),
                "topics": [state_topic_for("kick-mine")],  # topic collision
            }
        ]
    )
    restored = await restore(soul, "kick-mine")
    assert restored is None


async def test_restore_forward_compat_drops_unknown_fields():
    """An old-format memory containing extra keys must load cleanly."""
    blob = json.dumps({
        "kickoff_task_id": "k",
        "current_wave": 1,
        "unknown_future_field": 42,
        "cumulative_spend_usd": 7.5,
    })
    soul = _FakeSoul(initial=[
        {"content": blob, "topics": [state_topic_for("k")]},
    ])
    restored = await restore(soul, "k")
    assert restored is not None
    assert restored.current_wave == 1
    assert abs(restored.cumulative_spend_usd - 7.5) < 1e-9


async def test_event_cap_truncates_to_max():
    s = OrchestratorState(kickoff_task_id="k")
    for i in range(s.MAX_EVENTS + 25):
        s.record_event("tick", i=i)
    assert len(s.events) == s.MAX_EVENTS
    assert s.events[-1]["i"] == s.MAX_EVENTS + 25 - 1
