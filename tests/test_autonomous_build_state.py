"""AB-04 tests: state checkpoint/restore.

`OrchestratorState.to_json` + `from_json` round-trips. Soul memory read
path tolerates both list and {"memories": [...]} shapes.

Also covers SAL-2890 Fix D: gate-ACK persistence helpers
(``record_gate_ack`` / ``lookup_gate_ack`` / ``is_gate_ack_fresh``).
"""

from __future__ import annotations

import json
import time

from alfred_coo.autonomous_build.state import (
    GATE_ACK_FRESHNESS_SEC,
    GateAckRecord,
    OrchestratorState,
    checkpoint,
    gate_ack_topic_for,
    is_gate_ack_fresh,
    lookup_gate_ack,
    record_gate_ack,
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


# ── SAL-2890 Fix D: gate-ACK persistence ─────────────────────────────────


def test_gate_ack_topic_format_is_stable():
    """Topic key must match the SAL-2890 spec literally — soul-svc
    consumers (other Alfred sessions, dashboards) may key off this.
    """
    topic = gate_ack_topic_for("proj-uuid-123", "SS-08")
    assert topic == "autonomous_build:gate_ack:proj-uuid-123:SS-08"


def test_gate_ack_record_round_trips_full_payload():
    rec = GateAckRecord(
        linear_project_id="proj-1",
        gate_name="SS-08",
        acked_at="2026-04-27T00:48:00Z",
        acked_by_user_id="U0AH88KHZ4H",
        ack_message_ts="1777242958.524919",
        ack_message_text="approved SS-08",
    )
    blob = rec.to_json()
    parsed = GateAckRecord.from_json(blob)
    assert parsed == rec


def test_gate_ack_record_drops_unknown_keys_for_forward_compat():
    blob = json.dumps({
        "linear_project_id": "proj-1",
        "gate_name": "SS-08",
        "acked_at": "2026-04-27T00:00:00Z",
        "future_field": "ignore me",
    })
    rec = GateAckRecord.from_json(blob)
    assert rec.linear_project_id == "proj-1"
    assert rec.gate_name == "SS-08"
    assert rec.acked_by_user_id is None


async def test_record_gate_ack_writes_under_canonical_topic():
    soul = _FakeSoul()
    resp = await record_gate_ack(
        soul,
        linear_project_id="proj-1",
        gate_name="SS-08",
        acked_by_user_id="U0AH88KHZ4H",
        ack_message_ts="1777.42",
        ack_message_text="approved",
    )
    assert resp is not None
    assert len(soul.writes) == 1
    write = soul.writes[0]
    assert "autonomous_build:gate_ack:proj-1:SS-08" in write["topics"]
    payload = json.loads(write["content"])
    assert payload["linear_project_id"] == "proj-1"
    assert payload["gate_name"] == "SS-08"
    assert payload["acked_by_user_id"] == "U0AH88KHZ4H"
    assert payload["ack_message_ts"] == "1777.42"
    assert payload["ack_message_text"] == "approved"
    # Default acked_at must be set + parseable.
    assert payload["acked_at"]
    time.strptime(payload["acked_at"], "%Y-%m-%dT%H:%M:%SZ")


async def test_record_gate_ack_skips_when_project_id_empty():
    soul = _FakeSoul()
    resp = await record_gate_ack(
        soul, linear_project_id="", gate_name="SS-08",
    )
    assert resp is None
    assert soul.writes == []


async def test_record_gate_ack_skips_when_soul_is_none():
    resp = await record_gate_ack(
        None, linear_project_id="p", gate_name="SS-08",
    )
    assert resp is None


async def test_lookup_gate_ack_returns_record_when_present():
    soul = _FakeSoul()
    await record_gate_ack(
        soul,
        linear_project_id="proj-1",
        gate_name="SS-08",
        acked_by_user_id="U0AH88KHZ4H",
        ack_message_ts="1777.42",
        ack_message_text="approved",
    )
    found = await lookup_gate_ack(
        soul, linear_project_id="proj-1", gate_name="SS-08",
    )
    assert found is not None
    assert found.linear_project_id == "proj-1"
    assert found.gate_name == "SS-08"
    assert found.acked_by_user_id == "U0AH88KHZ4H"


async def test_lookup_gate_ack_returns_none_when_absent():
    soul = _FakeSoul()
    found = await lookup_gate_ack(
        soul, linear_project_id="proj-empty", gate_name="SS-08",
    )
    assert found is None


async def test_lookup_gate_ack_ignores_mismatched_project():
    """Belt-and-braces: if a topic collision lands a record under the
    wrong project key, lookup must reject it.
    """
    soul = _FakeSoul(initial=[
        {
            "content": json.dumps({
                "linear_project_id": "wrong-proj",
                "gate_name": "SS-08",
                "acked_at": "2026-04-27T00:00:00Z",
            }),
            "topics": [gate_ack_topic_for("proj-1", "SS-08")],
        }
    ])
    found = await lookup_gate_ack(
        soul, linear_project_id="proj-1", gate_name="SS-08",
    )
    assert found is None


def test_is_gate_ack_fresh_within_window():
    rec = GateAckRecord(
        linear_project_id="p",
        gate_name="SS-08",
        acked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    assert is_gate_ack_fresh(rec) is True


def test_is_gate_ack_fresh_rejects_stale():
    rec = GateAckRecord(
        linear_project_id="p",
        gate_name="SS-08",
        acked_at="2020-01-01T00:00:00Z",
    )
    assert is_gate_ack_fresh(rec) is False


def test_is_gate_ack_fresh_handles_malformed_timestamp():
    rec = GateAckRecord(
        linear_project_id="p",
        gate_name="SS-08",
        acked_at="not-a-timestamp",
    )
    assert is_gate_ack_fresh(rec) is False


def test_gate_ack_freshness_window_is_thirty_days():
    """Spec lock: 30-day window per SAL-2890."""
    assert GATE_ACK_FRESHNESS_SEC == 30 * 24 * 60 * 60
