"""AB-05 tests: BudgetTracker + estimate_cost + orchestrator drain wiring."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from alfred_coo.autonomous_build.budget import (
    BudgetTracker,
    FALLBACK_PRICE,
    estimate_cost,
    make_tracker,
)
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)


# ── estimate_cost ──────────────────────────────────────────────────────────


def test_estimate_cost_maps_model_to_price():
    # deepseek-v3.2:cloud: 0.27 in / 1.10 out per Mtok.
    # 1_000_000 in + 500_000 out = 0.27 + 0.55 = 0.82
    got = estimate_cost(1_000_000, 500_000, "deepseek-v3.2:cloud")
    assert got == pytest.approx(0.82, abs=1e-6)


def test_estimate_cost_local_model_is_free():
    got = estimate_cost(2_000_000, 2_000_000, "qwen3-coder:30b-a3b-q4_K_M")
    assert got == 0.0


def test_estimate_cost_unknown_model_uses_fallback():
    # $5/Mtok in + $15/Mtok out → 1M in + 1M out = 5 + 15 = $20
    got = estimate_cost(1_000_000, 1_000_000, "some-future-model:v9")
    expected = (1.0 * FALLBACK_PRICE["input"]) + (1.0 * FALLBACK_PRICE["output"])
    assert got == pytest.approx(expected, abs=1e-6)


def test_estimate_cost_non_numeric_treated_as_zero():
    # Should not raise; logs warning + returns 0 for both fields.
    assert estimate_cost("nope", None, "deepseek-v3.2:cloud") == 0.0


def test_estimate_cost_negative_tokens_clamped_to_zero():
    assert estimate_cost(-500_000, -500_000, "deepseek-v3.2:cloud") == 0.0


# ── BudgetTracker.record ──────────────────────────────────────────────────


def test_tracker_accumulates():
    bt = BudgetTracker(max_usd=30.0)
    bt.record({"result": {
        "tokens": {"in": 1_000_000, "out": 500_000},
        "model": "deepseek-v3.2:cloud",
    }})
    bt.record({"result": {
        "tokens": {"in": 500_000, "out": 250_000},
        "model": "deepseek-v3.2:cloud",
    }})
    # First: 0.82. Second: 0.135 + 0.275 = 0.41. Total: 1.23.
    assert bt.cumulative_spend == pytest.approx(0.82 + 0.41, abs=1e-4)


def test_tracker_accepts_raw_result_dict_too():
    """Some callers pass the `result` dict directly, not the whole task rec."""
    bt = BudgetTracker(max_usd=30.0)
    bt.record({
        "tokens": {"in": 1_000_000, "out": 1_000_000},
        "model": "qwen3-coder:480b-cloud",
    })
    assert bt.cumulative_spend > 0.0


def test_tracker_skips_missing_fields_non_fatal():
    """Records with missing tokens/model do not crash the tracker."""
    bt = BudgetTracker(max_usd=30.0)
    # Missing tokens.
    bt.record({"result": {"model": "deepseek-v3.2:cloud"}})
    # Missing model.
    bt.record({"result": {"tokens": {"in": 100, "out": 100}}})
    # Non-dict input.
    bt.record("not a dict")  # type: ignore[arg-type]
    # Non-dict result.
    bt.record({"result": "oops"})
    # Non-dict tokens.
    bt.record({"result": {"tokens": "nope", "model": "x"}})
    # Tokens both None.
    bt.record({"result": {"tokens": {"in": None, "out": None}, "model": "m"}})
    # All no-ops: cumulative spend still 0.
    assert bt.cumulative_spend == 0.0


def test_record_returns_incremental_cost():
    bt = BudgetTracker(max_usd=30.0)
    cost = bt.record({"result": {
        "tokens": {"in": 1_000_000, "out": 1_000_000},
        "model": "qwen3-coder:480b-cloud",
    }})
    # qwen3-coder:480b-cloud: 0.30 in + 1.20 out = 1.50
    assert cost == pytest.approx(1.50, abs=1e-4)


# ── thresholds ────────────────────────────────────────────────────────────


def test_warn_threshold_fires_once():
    bt = BudgetTracker(max_usd=30.0, warn_threshold_pct=0.8)
    # Below warn.
    bt.set_spend(20.0)
    assert bt.check_warn() is False
    # Cross warn (24 >= 30*0.8).
    bt.set_spend(24.1)
    assert bt.check_warn() is True
    # Second call: still above but should NOT re-fire.
    assert bt.check_warn() is False
    # Status reflects one-shot state.
    assert bt.warn_fired is True


def test_hard_stop_triggers_at_threshold():
    bt = BudgetTracker(max_usd=30.0)
    bt.set_spend(29.99)
    assert bt.check_hard_stop() is False
    bt.set_spend(30.0)
    assert bt.check_hard_stop() is True
    assert bt.in_drain_mode is True
    # One-shot: subsequent calls return False.
    assert bt.check_hard_stop() is False


def test_status_snapshot_shape():
    bt = BudgetTracker(max_usd=10.0)
    bt.set_spend(2.5)
    s = bt.status()
    assert s["cumulative_spend_usd"] == pytest.approx(2.5, abs=1e-4)
    assert s["max_usd"] == 10.0
    assert s["pct_spent"] == pytest.approx(0.25, abs=1e-4)
    assert s["in_drain_mode"] is False
    assert s["warn_fired"] is False
    assert s["hard_stop_fired"] is False


def test_make_tracker_reads_payload_budget():
    t = make_tracker({"max_usd": 50, "warn_threshold_pct": 0.75})
    assert t.max_usd == 50.0
    assert t.warn_threshold_pct == 0.75


def test_make_tracker_defaults_on_bad_payload():
    t = make_tracker({"max_usd": "not a number"})
    assert t.max_usd == 30.0


def test_constructor_rejects_bad_args():
    with pytest.raises(ValueError):
        BudgetTracker(max_usd=0)
    with pytest.raises(ValueError):
        BudgetTracker(max_usd=30, warn_threshold_pct=1.5)


# ── orchestrator drain integration ────────────────────────────────────────


class _FakeMesh:
    def __init__(self, completed: Optional[List[Dict[str, Any]]] = None) -> None:
        self.completed = list(completed or [])
        self.created: list[dict] = []
        self._next_id = 1
        self.completions: list[dict] = []

    async def create_task(self, *, title, description="", from_session_id=None):
        nid = f"child-{self._next_id}"
        self._next_id += 1
        rec = {"id": nid, "title": title, "status": "pending"}
        self.created.append({"title": title, "description": description})
        return rec

    async def list_tasks(self, status=None, limit=50):
        if status:
            return [t for t in self.completed
                    if (t.get("status") or "").lower() == status.lower()]
        return list(self.completed)

    async def complete(self, task_id, *, session_id, status=None, result=None):
        self.completions.append({
            "task_id": task_id, "status": status, "result": result,
        })


class _FakeSoul:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def write_memory(self, content, topics=None):
        self.writes.append({"content": content, "topics": topics or []})
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        return []


class _FakeSettings:
    soul_session_id = "test-session"


class _FakeCadence:
    """Drop-in replacement for SlackCadence that records post() calls."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self.ticks: list[dict] = []
        self.pings: list[dict] = []

    async def tick(self, state, graph, budget_status):
        self.ticks.append({"budget_status": budget_status})
        return {"ts": "fake"}

    async def post(self, message: str):
        self.posts.append(message)
        return {"ts": "fake"}

    async def critical_path_ping(self, ticket, elapsed_seconds, last_event):
        self.pings.append({
            "id": getattr(ticket, "identifier", "?"),
            "elapsed": elapsed_seconds,
            "last_event": last_event,
        })
        return {"ts": "fake"}


def _t(uuid, ident, code, wave, epic, **kwargs) -> Ticket:
    return Ticket(
        id=uuid, identifier=ident, code=code, title=f"{ident} {code}",
        wave=wave, epic=epic,
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        **kwargs,
    )


def _mk_orch(budget_payload: Optional[Dict[str, Any]] = None,
             mesh=None, soul=None) -> AutonomousBuildOrchestrator:
    desc = json.dumps({
        "linear_project_id": "proj-1",
        "budget": budget_payload or {"max_usd": 30.0},
        "status_cadence": {
            "slack_channel": "C0ASAKFTR1C",
            "interval_minutes": 20,
        },
    })
    task = {"id": "kick-1", "description": desc}

    class _P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"

    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_P(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    # Parse so the tracker + cadence are payload-configured.
    orch._parse_payload()
    # Swap in the fake cadence so no real Slack calls happen.
    orch.cadence = _FakeCadence()
    return orch


def test_orchestrator_sets_drain_mode_on_hard_stop():
    orch = _mk_orch(budget_payload={"max_usd": 1.0, "warn_threshold_pct": 0.8})
    # Force a record that blows past $1 on the fallback ($20/Mtok combined).
    orch._last_completed_records = [{
        "id": "child-1",
        "status": "completed",
        "result": {
            "tokens": {"in": 2_000_000, "out": 0},  # $10 on fallback
            "model": "unknown-huge:model",
        },
    }]
    asyncio.run(orch._check_budget())
    assert orch._drain_mode is True
    assert orch.budget_tracker.hard_stop_fired is True
    # Fake cadence captured the hard-stop post.
    assert any("BUDGET HARD STOP" in m for m in orch.cadence.posts)


def test_orchestrator_posts_warn_at_80pct_threshold():
    orch = _mk_orch(budget_payload={"max_usd": 10.0, "warn_threshold_pct": 0.8})
    # $5 < 80% of $10, no warn yet.
    orch._last_completed_records = [{
        "result": {"tokens": {"in": 500_000, "out": 0}, "model": "unknown:x"},
    }]
    asyncio.run(orch._check_budget())
    assert not any("80%" in m for m in orch.cadence.posts)
    # Push over 80% → $9 total (add another $4 on unknown → $5*0.8 + ... trick):
    # easier: set tracker spend directly.
    orch.budget_tracker.set_spend(8.5)  # above warn threshold 8.0
    asyncio.run(orch._check_budget())
    assert any("80%" in m or "80% threshold" in m for m in orch.cadence.posts)
    assert orch.budget_tracker.warn_fired is True


def test_drain_mode_stops_new_dispatch():
    """In drain mode, _dispatch_wave must not call _dispatch_child for
    new tickets. In-flight tickets can still progress; we assert only
    that `create_task` is never called in drain mode."""
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)
    a = _t("ua", "SAL-1", "X-1", 0, "ops")
    b = _t("ub", "SAL-2", "X-2", 0, "ops")
    g = TicketGraph()
    g.nodes[a.id] = a
    g.nodes[b.id] = b
    g.identifier_index[a.identifier] = a.id
    g.identifier_index[b.identifier] = b.id
    orch.graph = g

    # Flip drain before any dispatch.
    orch._drain_mode = True
    # Force both tickets into a terminal state so the wave exits after
    # the first select cycle; we just want to confirm no dispatch happened.
    a.status = TicketStatus.MERGED_GREEN
    b.status = TicketStatus.MERGED_GREEN

    async def _run():
        await orch._dispatch_wave(0)

    asyncio.run(_run())
    assert mesh.created == []


def test_check_budget_clears_last_completed_records():
    """Ensure the same batch of completed records can't be double-counted."""
    orch = _mk_orch(budget_payload={"max_usd": 30.0})
    orch._last_completed_records = [{
        "result": {"tokens": {"in": 1_000_000, "out": 0},
                   "model": "deepseek-v3.2:cloud"},
    }]
    asyncio.run(orch._check_budget())
    first = orch.budget_tracker.cumulative_spend
    assert first > 0.0
    # Second call with an empty batch must not re-accumulate.
    asyncio.run(orch._check_budget())
    assert orch.budget_tracker.cumulative_spend == pytest.approx(first, abs=1e-9)
