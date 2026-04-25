"""AB-04 tests: orchestrator wave scheduler + dispatch logic.

These tests drive `AutonomousBuildOrchestrator` through fake clients + a
hand-seeded ticket graph so we can assert concrete behaviour on the wave
gate, per-epic cap, max-parallel cap, dep resolution, critical-path
halting, and state checkpoint/restore integration.

We avoid the full `run()` flow in most tests (which would need Linear +
Supabase + Slack); instead we call internal methods directly with a pre-
built graph. The one integration-ish test at the bottom drives `run()`
end-to-end with every external call stubbed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from alfred_coo.autonomous_build.graph import (
    TERMINAL_STATES,
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    HintStatus,
    PathResult,
    TargetHint,
    VerificationResult,
    _TARGET_HINTS,
    _VERDICT_REQUEST_CHANGES_RE,
    _render_target_block,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeMesh:
    def __init__(self, created_responses: dict | None = None,
                 completed_tasks: list[dict] | None = None):
        self.created: list[dict] = []
        self._next_id = 1
        self._created_responses = created_responses or {}
        self.completed_tasks = list(completed_tasks or [])
        self.completions: list[dict] = []

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {"title": title, "description": description,
               "from_session_id": from_session_id}
        self.created.append(rec)
        # Deterministic id so tests can cross-reference.
        if title in self._created_responses:
            return self._created_responses[title]
        nid = f"child-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        if status:
            return [t for t in self.completed_tasks
                    if (t.get("status") or "").lower() == status.lower()]
        return list(self.completed_tasks)

    async def complete(self, task_id, *, session_id, status=None, result=None):
        self.completions.append({
            "task_id": task_id, "session_id": session_id,
            "status": status, "result": result,
        })


class _FakeSoul:
    def __init__(self):
        self.writes: list[dict] = []
        self.reads: list[dict] = []

    async def write_memory(self, content, topics=None):
        rec = {"content": content, "topics": topics or []}
        self.writes.append(rec)
        self.reads.insert(0, rec)
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        if topics:
            filtered = [m for m in self.reads
                        if any(t in (m.get("topics") or []) for t in topics)]
        else:
            filtered = list(self.reads)
        return filtered[:limit]


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


def _mk_persona():
    # Simple stand-in — the orchestrator only reads `.name`.
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"
    return P()


def _mk_orchestrator(
    kickoff_desc: dict | str = "",
    mesh=None,
    soul=None,
) -> AutonomousBuildOrchestrator:
    if isinstance(kickoff_desc, dict):
        kickoff_desc = json.dumps(kickoff_desc)
    task = {"id": "kick-abc", "title": "[persona:autonomous-build-a] kickoff",
            "description": kickoff_desc}
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _seed_graph(orch: AutonomousBuildOrchestrator, tickets: list[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _t(uuid, ident, code, wave, epic, **kwargs) -> Ticket:
    return Ticket(
        id=uuid, identifier=ident, code=code, title=f"{ident} {code}",
        wave=wave, epic=epic,
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        **kwargs,
    )


# ── Dep resolution + cap enforcement ───────────────────────────────────────


def test_dependency_respect_within_wave():
    """A blocks B: B not selected until A is merged_green."""
    orch = _mk_orchestrator()
    a = _t("ua", "SAL-1", "OPS-04", 1, "ops")
    b = _t("ub", "SAL-2", "OPS-05", 1, "ops", blocks_in=["ua"])
    a.blocks_out = ["ub"]
    _seed_graph(orch, [a, b])

    ready = orch._select_ready([a, b], in_flight=[])
    assert a in ready
    assert b not in ready
    assert b.status == TicketStatus.BLOCKED

    # Now A is green — B should become ready.
    a.status = TicketStatus.MERGED_GREEN
    ready = orch._select_ready([a, b], in_flight=[])
    assert a not in ready  # already terminal
    assert b in ready
    assert b.status == TicketStatus.PENDING  # unblocked


def test_per_epic_cap_enforced():
    """5 tiresias wave-1 tickets all ready, per_epic_cap=3 → only 3 dispatched."""
    orch = _mk_orchestrator()
    orch.per_epic_cap = 3
    orch.max_parallel_subs = 10  # not the constraint
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-0{i}", 1, "tiresias")
        for i in range(1, 6)
    ]
    _seed_graph(orch, tickets)

    ready = orch._select_ready(tickets, in_flight=[])
    assert len(ready) == 5  # all candidates
    # Simulate the dispatch loop honouring per-epic cap.
    in_flight: list = []
    dispatched: list = []
    for ticket in ready:
        if orch._epic_in_flight(ticket.epic, in_flight) >= orch.per_epic_cap:
            continue
        in_flight.append(ticket)
        dispatched.append(ticket)
    assert len(dispatched) == 3


def test_max_parallel_subs_enforced():
    """10 mixed-epic tickets ready, max_parallel_subs=6."""
    orch = _mk_orchestrator()
    orch.max_parallel_subs = 6
    orch.per_epic_cap = 10  # not the constraint
    tickets = []
    for i, epic in enumerate(
        ["tiresias", "aletheia", "fleet", "ops", "soul-gap",
         "tiresias", "aletheia", "fleet", "ops", "soul-gap"]
    ):
        tickets.append(_t(f"u{i}", f"SAL-{i}", f"X-{i}", 1, epic))
    _seed_graph(orch, tickets)

    ready = orch._select_ready(tickets, in_flight=[])
    assert len(ready) == 10
    in_flight: list = []
    for ticket in ready:
        if len(in_flight) >= orch.max_parallel_subs:
            break
        in_flight.append(ticket)
    assert len(in_flight) == 6


def test_select_ready_prioritises_critical_path():
    orch = _mk_orchestrator()
    a = _t("ua", "SAL-1", "TIR-01", 1, "tiresias", is_critical_path=False)
    b = _t("ub", "SAL-2", "TIR-02", 1, "tiresias", is_critical_path=True)
    _seed_graph(orch, [a, b])
    ready = orch._select_ready([a, b], in_flight=[])
    assert ready[0] is b


# ── Wave gate ──────────────────────────────────────────────────────────────


async def test_wave_gate_blocks_until_all_green(monkeypatch):
    """9 of 10 green, assert wait_for_wave_gate does NOT return until the 10th
    is green. We advance state between sleep ticks via a monkeypatched
    asyncio.sleep that flips ticket 10 after two ticks."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0  # hot loop in the test
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    # First 9 are green up front.
    for t in tickets[:9]:
        t.status = TicketStatus.MERGED_GREEN
    _seed_graph(orch, tickets)

    tick_count = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        tick_count["n"] += 1
        if tick_count["n"] >= 2:
            tickets[9].status = TicketStatus.MERGED_GREEN
        await real_sleep(0)

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", fake_sleep
    )
    await asyncio.wait_for(orch._wait_for_wave_gate(1), timeout=2.0)
    assert all(t.status == TicketStatus.MERGED_GREEN for t in tickets)
    assert tick_count["n"] >= 2, "gate exited before flipping ticket 10"


async def test_critical_path_failure_halts_program(monkeypatch):
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t("u1", "SAL-1", "TIR-01", 1, "tiresias", is_critical_path=True),
        _t("u2", "SAL-2", "TIR-02", 1, "tiresias"),
    ]
    tickets[0].status = TicketStatus.FAILED  # critical-path fail
    tickets[1].status = TicketStatus.MERGED_GREEN
    _seed_graph(orch, tickets)

    async def _nosleep(delay):
        return None
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", _nosleep
    )

    with pytest.raises(RuntimeError, match="critical-path"):
        await orch._wait_for_wave_gate(1)


async def test_noncritical_failure_logs_but_continues(monkeypatch):
    """A non-critical failure + >=90% green → soft-green, no raise."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    # 9 green + 1 non-cp fail = 90% exactly.
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:9]:
        t.status = TicketStatus.MERGED_GREEN
    tickets[9].status = TicketStatus.FAILED  # non-cp
    _seed_graph(orch, tickets)

    async def _nosleep(delay):
        return None
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", _nosleep
    )

    # No raise.
    await orch._wait_for_wave_gate(1)
    events = orch.state.events
    assert any(e["kind"] == "wave_soft_green" for e in events)


# ── AB-17-w: wave-gate green_ratio configurable + excused-from-denominator ─


async def _patch_nosleep(monkeypatch):
    async def _nosleep(delay):
        return None
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", _nosleep
    )


async def test_wave_gate_passes_when_threshold_met(monkeypatch):
    """AB-17-w · 9 green / 1 failed / 0 excused, default threshold 0.9 →
    soft-green pass (existing behaviour, no regression)."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:9]:
        t.status = TicketStatus.MERGED_GREEN
    tickets[9].status = TicketStatus.FAILED
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    await orch._wait_for_wave_gate(1)
    events = orch.state.events
    assert any(e["kind"] == "wave_soft_green" for e in events)


async def test_wave_gate_fails_when_threshold_unmet(monkeypatch):
    """AB-17-w · 7 green / 3 failed / 0 excused → 0.7 < 0.9 → raises."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:7]:
        t.status = TicketStatus.MERGED_GREEN
    for t in tickets[7:]:
        t.status = TicketStatus.FAILED
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match="green_ratio=0.70"):
        await orch._wait_for_wave_gate(1)
    events = orch.state.events
    assert any(e["kind"] == "wave_halt_below_soft_green" for e in events)


async def test_wave_gate_exempts_human_assigned(monkeypatch):
    """AB-17-w · 7 green / 3 failed (all human-assigned) → denominator 7 →
    7/7=1.0 → passes without raise. The 3 failures are excused from BOTH
    numerator and denominator, so the gate sees an all-green wave."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:7]:
        t.status = TicketStatus.MERGED_GREEN
    for t in tickets[7:]:
        t.status = TicketStatus.FAILED
        t.labels = ["human-assigned"]  # excuse from denominator
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    # No raise.
    await orch._wait_for_wave_gate(1)
    # Should land on the all-green path (denominator = 7, ratio = 1.0),
    # not soft-green (since `failed` after excusal is empty).
    events = orch.state.events
    kinds = [e["kind"] for e in events]
    assert "wave_all_green" in kinds
    assert "wave_halt_below_soft_green" not in kinds
    # Excused count should be reported on the all-green event for ops
    # visibility (3 human-assigned tickets sat out this wave).
    all_green_evt = next(e for e in events if e["kind"] == "wave_all_green")
    assert all_green_evt.get("excused_count") == 3


async def test_wave_gate_skips_when_all_excused(monkeypatch):
    """AB-17-w · 0 green / 5 path_conflict (all excused) → denominator 0 →
    skip green-ratio check entirely → passes."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(5)
    ]
    # All five reached terminal as FAILED but verification flagged each
    # as PATH_CONFLICT — the orchestrator never had an actionable target.
    for t in tickets:
        t.status = TicketStatus.FAILED
        # Seed verified_hints so _is_wave_gate_excused excuses them via
        # axis 2 (PATH_CONFLICT verification result).
        orch._verified_hints[t.code] = VerificationResult(
            code=t.code,
            hint=None,
            status=HintStatus.PATH_CONFLICT,
            repo_exists=True,
            path_results=(),
            error="path conflict",
        )
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    # No raise — wave is by-definition successful when every ticket is
    # excused.
    await orch._wait_for_wave_gate(1)
    events = orch.state.events
    kinds = [e["kind"] for e in events]
    assert "wave_all_excused" in kinds
    assert "wave_halt_below_soft_green" not in kinds
    evt = next(e for e in events if e["kind"] == "wave_all_excused")
    assert evt.get("excused_count") == 5


async def test_wave_gate_threshold_override(monkeypatch):
    """AB-17-w · payload threshold=0.6, 7 green / 3 failed / 0 excused →
    0.7 ≥ 0.6 → soft-green pass (would raise on default 0.9)."""
    payload = {
        "linear_project_id": "proj-abc",
        "wave_green_ratio_threshold": 0.6,
    }
    orch = _mk_orchestrator(kickoff_desc=payload)
    orch._parse_payload()  # apply override onto self
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:7]:
        t.status = TicketStatus.MERGED_GREEN
    for t in tickets[7:]:
        t.status = TicketStatus.FAILED
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    # Threshold 0.6 ≤ 0.7, so soft-green passes (no raise).
    await orch._wait_for_wave_gate(1)
    events = orch.state.events
    soft = next(
        (e for e in events if e["kind"] == "wave_soft_green"), None
    )
    assert soft is not None
    assert soft.get("threshold") == pytest.approx(0.6)


async def test_wave_gate_combination(monkeypatch):
    """AB-17-w · 7 green / 3 failed where 1 failure is human-assigned →
    denominator = 9 (10 - 1 excused), green = 7, ratio = 7/9 ≈ 0.78 → below
    default 0.9 → raises. Verifies excusal applies BEFORE the ratio test
    (not after) — i.e. removing the excused ticket from the denominator,
    not from the failed list alone."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 1, "tiresias")
        for i in range(10)
    ]
    for t in tickets[:7]:
        t.status = TicketStatus.MERGED_GREEN
    for t in tickets[7:]:
        t.status = TicketStatus.FAILED
    # Excuse exactly one of the three failures.
    tickets[9].labels = ["human-assigned"]
    _seed_graph(orch, tickets)
    await _patch_nosleep(monkeypatch)

    with pytest.raises(RuntimeError, match=r"green_ratio=0\.78"):
        await orch._wait_for_wave_gate(1)
    events = orch.state.events
    halt = next(
        e for e in events if e["kind"] == "wave_halt_below_soft_green"
    )
    assert halt.get("excused_count") == 1
    assert halt.get("green_ratio") == pytest.approx(7 / 9, abs=1e-3)


def test_parse_payload_threshold_default_and_override():
    """AB-17-w · _parse_payload should default to SOFT_GREEN_THRESHOLD
    (0.9) when the field is absent and apply the float override when
    present. Non-numeric values are ignored with a warning, falling back
    to the default."""
    # Default.
    orch = _mk_orchestrator(kickoff_desc={"linear_project_id": "p1"})
    orch._parse_payload()
    assert orch.wave_green_ratio_threshold == pytest.approx(0.9)

    # Explicit override.
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "wave_green_ratio_threshold": 0.75,
    })
    orch._parse_payload()
    assert orch.wave_green_ratio_threshold == pytest.approx(0.75)

    # Non-numeric is ignored; default kept.
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p1",
        "wave_green_ratio_threshold": "not-a-float",
    })
    orch._parse_payload()
    assert orch.wave_green_ratio_threshold == pytest.approx(0.9)


# ── PR extraction ───────────────────────────────────────────────────────────


def test_extract_pr_url_from_summary():
    result = {"summary": "opened https://github.com/salucallc/foo/pull/42 today"}
    url = AutonomousBuildOrchestrator._extract_pr_url(result)
    assert url == "https://github.com/salucallc/foo/pull/42"


def test_extract_pr_url_from_tool_calls():
    result = {
        "summary": "done",
        "tool_calls": [
            {"name": "propose_pr",
             "result": {"pr_url": "https://github.com/salucallc/bar/pull/7"}}
        ],
    }
    url = AutonomousBuildOrchestrator._extract_pr_url(result)
    assert url == "https://github.com/salucallc/bar/pull/7"


def test_extract_pr_url_returns_none_when_absent():
    result = {"summary": "no pr here"}
    assert AutonomousBuildOrchestrator._extract_pr_url(result) is None


# ── Integration-ish: run() end-to-end with stubs ───────────────────────────


async def test_run_integration_dry_run_harness(monkeypatch):
    """Drive `run()` with a 3-ticket payload (wave 0 x2, wave 1 x1).

    Every external call is stubbed. Assert:
      - orchestrator dispatches the 2 wave-0 tickets
      - after they "complete" (fake mesh returns completed records), the
        wave-1 ticket gets dispatched
      - kickoff mesh task ends up completed with a summary
    """
    kickoff_payload = {
        "linear_project_id": "proj-test",
        "concurrency": {"max_parallel_subs": 6, "per_epic_cap": 3},
        "budget": {"max_usd": 30},
        "wave_order": [0, 1],
        "on_all_green": [],
        "status_cadence": {"interval_minutes": 20},
    }

    mesh = _FakeMesh()
    soul = _FakeSoul()
    orch = _mk_orchestrator(
        kickoff_desc=kickoff_payload,
        mesh=mesh,
        soul=soul,
    )
    orch.poll_sleep_sec = 0

    # Stub graph build — inject fetchers that return our canned issues.
    issues = [
        {"id": "u0a", "identifier": "SAL-1", "title": "TIR-01 a",
         "labels": ["wave-0", "tiresias"], "estimate": 1,
         "state": {"name": "Backlog"}, "relations": []},
        {"id": "u0b", "identifier": "SAL-2", "title": "TIR-02 b",
         "labels": ["wave-0", "tiresias"], "estimate": 1,
         "state": {"name": "Backlog"}, "relations": []},
        {"id": "u1a", "identifier": "SAL-3", "title": "TIR-03 c",
         "labels": ["wave-1", "tiresias"], "estimate": 1,
         "state": {"name": "Backlog"}, "relations": []},
    ]

    async def fake_list(project_id, limit=250):
        return {"issues": issues, "total": 3, "truncated": False}

    async def fake_rel(issue_id):
        return {"blocks": [], "blocked_by": [], "related": []}

    orch._list_project_issues = fake_list
    orch._get_issue_relations = fake_rel

    # Ensure _update_linear_state is a no-op — tools may not be importable
    # in the test env without LINEAR_API_KEY.
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    # Stub both wave-start verification and SAL-2787 per-dispatch
    # verification so the harness doesn't make live GitHub calls.
    async def _stub_verify_hint(code, hint):
        return VerificationResult(
            code=code,
            hint=hint,
            status=HintStatus.UNVERIFIED,
            repo_exists=False,
            path_results=(),
            error="stubbed in test",
        )
    monkeypatch.setattr(orch, "_verify_hint", _stub_verify_hint)

    # Simulate an instant APPROVE + merge when the orchestrator dispatches
    # a review task. In production this would take a real review loop; the
    # dry-run harness short-circuits straight to MERGED_GREEN so the wave
    # gate can advance. `_fake_review` sets MERGED_GREEN then raises — the
    # raise prevents the orchestrator's next line (`ticket.status =
    # REVIEWING`) from overwriting the terminal status. This is the only
    # way to simulate test-side review completion without adding a new
    # hook to the production code path (follow-up: AB-08 REVIEWING→
    # MERGED_GREEN transition logic).
    from alfred_coo.autonomous_build.graph import TicketStatus as _TS
    class _FakeReviewDone(Exception):
        pass
    async def _fake_review(ticket):
        ticket.status = _TS.MERGED_GREEN
        raise _FakeReviewDone
    monkeypatch.setattr(orch, "_dispatch_review", _fake_review)

    # Drive mesh.list_tasks: as soon as a child is dispatched, mark it
    # completed with a fake PR URL so the orchestrator transitions to
    # PR_OPEN → (review is a no-op here) → MERGED_GREEN via the PR path.
    # (Children without PR URLs now mark FAILED, not MERGED_GREEN — see
    # test_run_no_pr_child_marks_failed for that path.)
    original_list = mesh.list_tasks

    async def driving_list(status=None, limit=50):
        # Mark every dispatched child completed by title ordering.
        for idx, rec in enumerate(mesh.created, start=1):
            cid = f"child-{idx}"
            if any(c["id"] == cid for c in mesh.completed_tasks):
                continue
            mesh.completed_tasks.append({
                "id": cid,
                "title": rec["title"],
                "status": "completed",
                "result": {
                    "summary": f"done; PR https://github.com/salucallc/x/pull/{idx}",
                    "pr_url": f"https://github.com/salucallc/x/pull/{idx}",
                },
            })
        return await original_list(status=status, limit=limit)

    mesh.list_tasks = driving_list  # type: ignore[assignment]

    # Run the orchestrator with a timeout so a bug doesn't hang CI.
    await asyncio.wait_for(orch.run(), timeout=5.0)

    # 3 child tasks dispatched (one per ticket).
    assert len(mesh.created) == 3, [r["title"] for r in mesh.created]
    # Wave 0 tickets dispatched first, then wave 1.
    # Assert ordering: wave-1 appears after both wave-0.
    titles = [r["title"] for r in mesh.created]
    assert all("wave-0" in t for t in titles[:2])
    assert "wave-1" in titles[2]

    # Kickoff got completed.
    assert len(mesh.completions) == 1
    comp = mesh.completions[0]
    assert comp["task_id"] == "kick-abc"
    assert comp["status"] in (None, "completed")
    assert "merged_green" in comp["result"]["summary"]

    # State was checkpointed at least once.
    assert soul.writes, "state checkpoint never ran"


def test_child_task_body_uses_repo_raw_plan_url():
    """Regression: child task bodies must reference plan docs via
    https://raw.githubusercontent.com/salucallc/alfred-coo-svc/main/plans/v1-ga/...
    NOT a minipc-local ``Z:/_planning/...`` path. Children run on Oracle
    and can't see the Windows share, so Z:/ references caused them to
    escalate to #batcave with "plan doc not found" errors (2026-04-23).
    """
    expected_by_epic = {
        "tiresias": "A_tiresias_in_appliance.md",
        "aletheia": "B_aletheia_daemon.md",
        "fleet": "C_fleet_mode_endpoint.md",
        "ops": "D_ops_layer.md",
        "soul-gap": "E_soul_svc_gaps.md",
    }
    base = (
        "https://raw.githubusercontent.com/salucallc/alfred-coo-svc/main/"
        "plans/v1-ga"
    )
    orch = _mk_orchestrator()
    for epic, filename in expected_by_epic.items():
        ticket = _t(f"u-{epic}", f"SAL-{epic}", f"{epic.upper()}-01", 1, epic)
        body = orch._child_task_body(ticket)
        expected_url = f"{base}/{filename}"
        assert expected_url in body, (
            f"epic={epic}: expected {expected_url!r} in child body, got:\n"
            f"{body}"
        )
        assert "Z:/_planning" not in body, (
            f"epic={epic}: child body still references minipc-only "
            f"Z:/_planning path:\n{body}"
        )
        assert "raw.githubusercontent.com" in body
        assert "http_get" in body, (
            "child body should instruct the sub to fetch the plan via "
            "http_get"
        )

    # Unknown epic falls back to the autonomous_build gap-closer plan (G),
    # which is safer than a 404 fallback.
    unknown = _t("u-x", "SAL-X", "X-01", 1, "not-a-real-epic")
    body = orch._child_task_body(unknown)
    assert f"{base}/G_autonomous_build_gap_closers.md" in body


# ── AB-14 (SAL-2699): child body must emit Plan-doc code grep anchor ───────


def test_child_task_body_emits_plan_doc_code_line():
    """AB-14: children must see a `Plan-doc code: <code>` line verbatim
    so they can grep the plan-doc markdown for the exact section header
    (``F08``, ``OPS-01``, ``C-26``, ...). Without this, the live run on
    2026-04-23 showed SAL-2616's F08 child fabricating scope because it
    had no stable anchor in the plan doc.
    """
    orch = _mk_orchestrator()
    ticket = _t("u-1", "SAL-2616", "F08", 1, "fleet")
    body = orch._child_task_body(ticket)
    assert (
        "Plan-doc code: F08 "
        "(search for this string in the plan-doc markdown)"
    ) in body, f"missing plan-doc-code line in body:\n{body}"


def test_child_task_body_unparseable_code_emits_escalate_line():
    """AB-14: empty ticket.code means the orchestrator failed to parse
    a plan-doc anchor from the title. The child MUST NOT guess; it must
    escalate per Step 0 of its persona protocol. The body makes that
    explicit instead of silently omitting the Plan-doc code line."""
    orch = _mk_orchestrator()
    ticket = _t("u-bad", "SAL-BAD", "", 1, "ops")
    body = orch._child_task_body(ticket)
    assert (
        "Plan-doc code: (unparseable — escalate per Step 0 of your "
        "persona protocol)"
    ) in body, f"missing escalate fallback line in body:\n{body}"


async def test_run_missing_linear_project_id_fails_kickoff():
    """Payload without linear_project_id → orchestrator fails the kickoff
    cleanly instead of crashing."""
    mesh = _FakeMesh()
    orch = _mk_orchestrator(kickoff_desc={"budget": {"max_usd": 30}}, mesh=mesh)
    await orch.run()
    assert len(mesh.completions) == 1
    assert mesh.completions[0]["status"] == "failed"
    assert "linear_project_id" in mesh.completions[0]["result"]["error"]


async def test_poll_children_marks_failed_when_no_pr_url(monkeypatch):
    """Regression: a child task that completes WITHOUT a PR URL in its
    result is a silent failure (model never called propose_pr), not a
    success. Orchestrator must mark the ticket FAILED and push Linear
    back to Backlog — NOT MERGED_GREEN + Done. (2026-04-23 bug: 12
    false-greens observed on first live run.)"""
    mesh = _FakeMesh()
    orch = _mk_orchestrator(
        kickoff_desc={
            "linear_project_id": "p",
            "budget": {"max_usd": 30},
            "wave_order": [0],
            "on_all_green": [],
        },
        mesh=mesh,
    )

    # One wave-0 ticket, dispatched, awaiting completion.
    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-1"
    _seed_graph(orch, [t])

    # Track Linear state transitions the orchestrator requests.
    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Fake mesh returns a "completed" child with no PR URL in result.
    mesh.completed_tasks.append({
        "id": "child-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-1 TIR-01 ...",
        "status": "completed",
        "result": {"summary": "I considered the task but did not open a PR"},
    })

    await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED, got {t.status}; no-PR children must NOT be "
        f"marked MERGED_GREEN (regression from 2026-04-23 bug)"
    )
    assert ("SAL-1", "Backlog") in linear_calls, (
        f"expected Linear rollback to Backlog, got: {linear_calls}"
    )
    assert not any(state == "Done" for _, state in linear_calls), (
        f"ticket was falsely moved to Done: {linear_calls}"
    )


# ── AB-13: ## Target block + _TARGET_HINTS table (Plan H §2 G-2) ────────────


def test_target_hint_dataclass_roundtrip():
    """TargetHint is a frozen dataclass; fields round-trip and it is
    hashable (so it can sit in a tuple / set if future code needs it)."""
    h = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yaml",),
        branch_hint="feature/sal-2634-mc-ops-network",
        notes="add mc-ops network + 4 volumes",
    )
    assert h.owner == "salucallc"
    assert h.repo == "alfred-coo-svc"
    assert h.paths == ("deploy/appliance/docker-compose.yaml",)
    assert h.base_branch == "main"  # default
    assert h.branch_hint == "feature/sal-2634-mc-ops-network"
    assert h.notes == "add mc-ops network + 4 volumes"
    # Frozen dataclass: hashable + immutable
    {h}  # must not raise
    with pytest.raises(Exception):
        h.owner = "someone-else"  # type: ignore[misc]


def test_child_task_body_renders_target_block_for_ops_01():
    """Plan H §2 G-2 regression: OPS-01's child body must contain a
    ``## Target`` block pinning `salucallc/alfred-coo-svc` at
    `deploy/appliance/docker-compose.yml` (AB-17-a: typo-fix from the
    original `.yaml` that did not match the real file on main). The
    original live run on 2026-04-24 produced a phantom root
    `docker-compose.yml` because this was not pinned (PR #32, SAL-2634)."""
    orch = _mk_orchestrator()
    ticket = _t("u-ops-01", "SAL-2634", "OPS-01", 0, "ops", size="S")
    body = orch._child_task_body(ticket)

    assert "## Target" in body
    assert "owner: salucallc" in body
    assert "repo:  alfred-coo-svc" in body
    assert "deploy/appliance/docker-compose.yml" in body
    assert "base_branch: main" in body
    assert "branch_hint: feature/sal-2634-mc-ops-network" in body
    assert "notes: add mc-ops network + 4 volumes" in body
    # The Target block must land BEFORE the APE/V section so the sub
    # reads target-pinning before the acceptance criteria.
    assert body.index("## Target") < body.index("## Acceptance (APE/V)")
    # Must NOT degrade to the unresolved fallback.
    assert "(unresolved" not in body


def test_child_task_body_target_block_unresolved_for_unmapped_code():
    """When `_TARGET_HINTS` has no entry for the ticket code, the body
    must render the ``(unresolved)`` escalation prompt rather than
    guessing — child handles Step-0 escalation per Plan H §5 R-d."""
    orch = _mk_orchestrator()
    ticket = _t("u-x", "SAL-9999", "ZZZ-99", 0, "other", size="M")
    body = orch._child_task_body(ticket)

    assert "## Target" in body
    assert "(unresolved" in body
    assert "linear_create_issue" in body
    assert "STOP" in body
    # No owner/repo leak when unresolved.
    assert "owner: salucallc" not in body.split("## Acceptance")[0]


def test_child_task_body_target_block_empty_code_unresolved():
    """If ticket.code is empty (e.g., `F`/`D`/`E` prefixes that pre-AB-14
    `_CODE_RE` drops), we must still emit an unresolved block, not crash."""
    orch = _mk_orchestrator()
    ticket = _t("u-nocode", "SAL-2616", "", 0, "fleet", size="M")
    body = orch._child_task_body(ticket)

    assert "## Target" in body
    assert "(unresolved" in body


def test_target_hints_populated_for_wave_0_epics():
    """Regression guard: the MVP wave-0 tickets for each major v1-GA epic
    must be resolvable. If someone deletes an entry by accident, CI
    catches it before the next live autonomous_build dispatch."""
    required_codes = [
        # Epic D: Ops
        "OPS-01", "OPS-02", "OPS-03",
        # Epic C/F: Fleet mode endpoint
        "F01", "F02", "F07", "F08",
        # Epic E: soul-svc gap closure
        "S-01", "S-02", "S-04", "S-09",
        # Epic A: Tiresias
        "TIR-01", "TIR-02", "TIR-07", "TIR-08",
    ]
    missing = [c for c in required_codes if c not in _TARGET_HINTS]
    assert not missing, (
        f"_TARGET_HINTS missing wave-0/1 codes: {missing}. "
        f"Plan H §2 G-2 requires every wave-0 ticket to be resolvable."
    )
    # Every hint must name a Saluca-org repo and satisfy the AB-17-a
    # invariant (at least one of paths / new_paths non-empty).
    for code, hint in _TARGET_HINTS.items():
        assert hint.owner == "salucallc", f"{code}: non-saluca owner"
        assert hint.repo, f"{code}: empty repo"
        assert hint.paths or hint.new_paths, (
            f"{code}: both paths and new_paths are empty"
        )
        all_paths = tuple(hint.paths) + tuple(hint.new_paths)
        assert all(p and not p.startswith("/") for p in all_paths), (
            f"{code}: absolute/empty path in {all_paths}"
        )


def test_render_target_block_f08_soul_lite():
    """F08 must pin `salucallc/soul-svc` — it's the new `soul-lite`
    service and the previous child guessed `alfred-coo-svc` (SAL-2616 /
    PR #31 regression, 2026-04-24).

    AB-17-a: F08 is pure-creation, so `soul_lite/*` paths now live in
    `new_paths` on the hint. AB-17-c will extend `_render_target_block`
    to emit a `new_paths:` section; until then the renderer only shows
    `paths:` + notes. We still validate repo + notes pinning here (the
    `soul_lite/` marker survives via `notes:`). A full `new_paths:`
    render assertion lands with AB-17-c."""
    block = _render_target_block("F08")
    assert "owner: salucallc" in block
    assert "repo:  soul-svc" in block
    # Notes still mention the soul-lite subpackage path so the child
    # can orient even before the renderer grows a new_paths section.
    assert "soul-lite" in block
    assert "(unresolved" not in block
    # And the hint data itself carries the four soul_lite paths in
    # the AB-17-a-canonical new_paths axis.
    assert "soul_lite/service.py" in _TARGET_HINTS["F08"].new_paths
    assert _TARGET_HINTS["F08"].paths == ()


def test_render_target_block_case_insensitive():
    """Plan docs use uppercase codes (`OPS-01`), but graph._CODE_RE may
    emit lowercase if the Linear title is lowercased. The lookup must be
    case-insensitive so we don't silently fall to (unresolved)."""
    upper = _render_target_block("OPS-01")
    lower = _render_target_block("ops-01")
    assert upper == lower
    assert "deploy/appliance/docker-compose.yml" in lower


# ── AB-17-a · schema extension + corrected _TARGET_HINTS ───────────────────
#
# Plan I §2.1–2.2 (`Z:/_planning/v1-ga/I_target_verification.md`). Data diff
# from `Z:/_planning/v1-ga/hints_audit_2026-04-24.md` §4. AB-17-b adds
# `_verify_hint` / `_verify_wave_hints`; AB-17-c extends `_render_target_block`
# to consume a `VerificationResult`. This ticket is schema + data only.


def test_target_hint_post_init_rejects_empty_paths_and_new_paths():
    """AB-17-a (Plan I §2.1): the invariant `len(paths) + len(new_paths) >= 1`
    must be enforced at dataclass construction so empty hints crash at
    module-import time rather than silently rendering a borked `## Target`
    block."""
    with pytest.raises(ValueError, match="at least one of paths or new_paths"):
        TargetHint(
            owner="salucallc",
            repo="alfred-coo-svc",
            paths=(),
            new_paths=(),
        )


def test_target_hint_accepts_new_paths_only():
    """AB-17-a: a pure-creation ticket (e.g. OPS-02's IMAGE_PINS.md) must
    be able to omit `paths` entirely and still construct successfully."""
    h = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    assert h.paths == ()
    assert h.new_paths == ("deploy/appliance/IMAGE_PINS.md",)
    assert h.base_branch == "main"
    # Frozen — still hashable so orchestrator state dicts work.
    {h}


def test_target_hint_accepts_both_paths_and_new_paths():
    """AB-17-a: mixed-mode ticket (e.g. F07 modifies main.py + creates
    persona_loader.py) must support both fields simultaneously."""
    h = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("src/alfred_coo/main.py",),
        new_paths=("src/alfred_coo/persona_loader.py",),
    )
    assert h.paths == ("src/alfred_coo/main.py",)
    assert h.new_paths == ("src/alfred_coo/persona_loader.py",)


def test_target_hints_ops_01_uses_yml_extension():
    """AB-17-a data fix (audit §4, C1 class): OPS-01's compose path is
    `docker-compose.yml` not `.yaml` — the real file on main uses `.yml`.
    The original table typo caused SAL-2634 child to fabricate a phantom
    root `docker-compose.yml` on 2026-04-24."""
    assert _TARGET_HINTS["OPS-01"].paths == (
        "deploy/appliance/docker-compose.yml",
    )
    # new_paths stays empty: OPS-01 is modify-only.
    assert _TARGET_HINTS["OPS-01"].new_paths == ()


def test_target_hints_ops_02_image_pins_in_new_paths():
    """AB-17-a data fix (audit §4, C4 class): OPS-02's `IMAGE_PINS.md` is
    a brand-new file created by this ticket per plan D §5 W1 #2. It must
    live in `new_paths` so AB-17-b's verifier asserts 404, not 200."""
    hint = _TARGET_HINTS["OPS-02"]
    assert "deploy/appliance/IMAGE_PINS.md" in hint.new_paths
    # And the compose file (which the ticket MODIFIES) must be in paths.
    assert "deploy/appliance/docker-compose.yml" in hint.paths
    # Must not appear under the wrong axis.
    assert "deploy/appliance/IMAGE_PINS.md" not in hint.paths


def test_target_hints_f01_flat_migrations_020():
    """AB-17-a data fix (audit §4, C3 class): soul-svc has a FLAT
    migrations/ dir (no db/ prefix) and the next free number on main is
    020 (005..019 exist). The original `db/migrations/0007_*.sql` hint
    was wrong on two axes simultaneously."""
    assert _TARGET_HINTS["F01"].paths == ()
    assert _TARGET_HINTS["F01"].new_paths == (
        "migrations/020_fleet_endpoints.sql",
    )


def test_target_hints_s04_uses_serve_py():
    """AB-17-a data fix (audit §4, S-04 weak-evidence fix): soul-svc's
    FastAPI entry point is `serve.py` not `main.py`. The original table
    named `main.py` which does not exist on main; a real S-04 child
    would 404 at Step 2 http_get and have to escalate."""
    hint = _TARGET_HINTS["S-04"]
    assert hint.paths == ("serve.py",)
    # Negative: no residual main.py reference leaked anywhere.
    assert "main.py" not in hint.paths
    assert "main.py" not in hint.new_paths
    # routers/metrics.py is new per plan E §3 item 4.
    assert "routers/metrics.py" in hint.new_paths
    assert "tests/test_metrics_endpoint.py" in hint.new_paths


def test_target_hints_entry_count_unchanged():
    """AB-19 adds 6 wave-0 entries (SS-01/02/06/09 + OPS-22 + ALT-01) to
    close the `no_hint: 6` gap observed in v8-full-v4 (mesh task 83dd216d,
    2026-04-24). Pre-AB-19 baseline was 16; post is 22."""
    assert len(_TARGET_HINTS) == 22


# ── AB-17-a · new result types (HintStatus / PathResult / VerificationResult)


def test_hint_status_enum_has_six_values():
    """Plan I §2.2: exactly six terminal states — OK, REPO_MISSING,
    PATH_MISSING, PATH_CONFLICT, UNVERIFIED, NO_HINT."""
    values = {m.value for m in HintStatus}
    assert values == {
        "ok",
        "repo_missing",
        "path_missing",
        "path_conflict",
        "unverified",
        "no_hint",
    }
    assert len(HintStatus) == 6
    # HintStatus is a str-Enum so JSON / soul-memory serialisation works.
    assert HintStatus.OK == "ok"
    assert isinstance(HintStatus.REPO_MISSING.value, str)


def test_path_result_dataclass_constructs():
    """Plan I §2.2: PathResult carries per-path expected / observed state
    plus the ok flag that the render decision table in §3 greps on."""
    pr = PathResult(
        path="deploy/appliance/docker-compose.yml",
        expected="exist",
        observed="exist",
        ok=True,
    )
    assert pr.path == "deploy/appliance/docker-compose.yml"
    assert pr.expected == "exist"
    assert pr.observed == "exist"
    assert pr.ok is True
    # Frozen → hashable.
    {pr}


def test_verification_result_dataclass_constructs():
    """Plan I §2.2: VerificationResult is the wave-boundary artifact the
    orchestrator stashes in `_verified_hints` and the renderer reads."""
    hint = _TARGET_HINTS["OPS-01"]
    vr = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=(
            PathResult(
                path="deploy/appliance/docker-compose.yml",
                expected="exist",
                observed="exist",
                ok=True,
            ),
        ),
        error=None,
        verified_at=1776997000.0,
    )
    assert vr.code == "OPS-01"
    assert vr.hint is hint
    assert vr.status is HintStatus.OK
    assert vr.repo_exists is True
    assert len(vr.path_results) == 1
    assert vr.error is None
    assert vr.verified_at == 1776997000.0
    # Frozen dataclass → hashable.
    {vr}


def test_verification_result_allows_none_hint_for_no_hint_status():
    """Plan I §2.2: when `status == NO_HINT` (code not in _TARGET_HINTS)
    the hint field must accept None — there's literally nothing to
    reference."""
    vr = VerificationResult(
        code="UNKNOWN-99",
        hint=None,
        status=HintStatus.NO_HINT,
        repo_exists=False,
        path_results=(),
        error="no hint for code UNKNOWN-99",
    )
    assert vr.hint is None
    assert vr.status is HintStatus.NO_HINT


# ── AB-17-b · _verify_hint + _verify_wave_hints + wave-start wiring ────────
#
# Plan I §1 — the core value of Plan I. Per-ticket hint verification runs at
# wave start; results are stashed on the orchestrator for AB-17-c to render
# into the ## Target block. These tests mock httpx.AsyncClient so the loop
# exercises the status-aggregation logic without hitting real GitHub.


class _FakeResp:
    """Minimal httpx.Response stand-in — only carries status_code + json()."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx as _httpx
            req = _httpx.Request("GET", "https://api.github.com/fake")
            raise _httpx.HTTPStatusError(
                f"{self.status_code}", request=req,
                response=_httpx.Response(self.status_code, request=req),
            )


class _FakeClient:
    """Script-driven AsyncClient replacement. `responses` is a dict mapping
    an endpoint-marker (substring of URL + ref) → list of _FakeResp. Each
    `.get()` pops the first response for the matching marker; leftover
    markers raise so tests catch missing fakes loudly.
    """

    # Class-level counter so concurrency tests can observe peak concurrency
    # regardless of how many FakeClient instances are constructed.
    _active = 0
    _peak = 0

    def __init__(self, *args, **kwargs):
        self._script: list | None = None  # populated by classmethod
        self._calls: list = []

    async def __aenter__(self):
        _FakeClient._active += 1
        if _FakeClient._active > _FakeClient._peak:
            _FakeClient._peak = _FakeClient._active
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _FakeClient._active -= 1
        return False

    async def get(self, url, headers=None, params=None):
        # Record for assertions + pull from the shared script.
        _FakeClient._calls_shared.append({"url": url, "params": params})
        script = _FakeClient._script_shared
        # Match policy:
        #   - Matcher keys starting with "repos/" match ONLY repo probes
        #     (URL ends with "/{owner}/{repo}", no /contents/ segment).
        #   - Matcher keys starting with "contents/" match ONLY contents
        #     probes (URL has "/contents/<path>"). Ref must match when
        #     matcher's ref_part is not None.
        ref = (params or {}).get("ref") if params else None
        is_contents_call = "/contents/" in url
        for matcher, resps in script.items():
            url_part, ref_part = matcher
            if url_part.startswith("contents/"):
                if not is_contents_call:
                    continue
                # url_part like "contents/README.md" → check that
                # "/<url_part>" appears in url (anchored after /contents/).
                if f"/{url_part}" not in url:
                    continue
                if ref_part is not None and ref_part != ref:
                    continue
            elif url_part.startswith("repos/"):
                if is_contents_call:
                    continue
                # url_part like "repos/owner/repo" — check url ends with it.
                if not url.endswith(f"/{url_part}"):
                    continue
            else:
                # Bare substring fallback for any other matcher shape.
                if url_part not in url:
                    continue
                if ref_part is not None and ref_part != ref:
                    continue
            if not resps:
                raise AssertionError(f"FakeClient script exhausted for {matcher}")
            return resps.pop(0)
        raise AssertionError(f"no FakeClient script entry matches url={url} ref={ref}")


# Shared state so the tests can wire up a script and inspect calls without
# needing to thread it through constructor kwargs.
_FakeClient._calls_shared = []
_FakeClient._script_shared = {}


def _reset_fake_client():
    _FakeClient._active = 0
    _FakeClient._peak = 0
    _FakeClient._calls_shared = []
    _FakeClient._script_shared = {}


def _install_fake_client(monkeypatch, script: dict):
    """Patch `httpx.AsyncClient` in the orchestrator module. `script` keys
    are (url_substring, ref_or_None) → list of _FakeResp.
    """
    _reset_fake_client()
    _FakeClient._script_shared = script
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.httpx.AsyncClient",
        _FakeClient,
    )
    # Also stub out the 2s sleep used by retry so tests don't stall.
    import alfred_coo.autonomous_build.orchestrator as _mod

    async def _fast_sleep(_delay):
        return None
    monkeypatch.setattr(_mod.asyncio, "sleep", _fast_sleep)


# Case 1: happy path — all paths exist → OK.
async def test_verify_hint_all_paths_exist_status_ok(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {"name": "alfred-coo-svc"})],
        ("contents/deploy/appliance/docker-compose.yml", "main"): [_FakeResp(200, {"type": "file"})],
        # new_paths expects absent → 404 is the happy outcome.
        ("contents/deploy/appliance/IMAGE_PINS.md", "main"): [_FakeResp(404)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("OPS-02", hint)

    assert vr.status is HintStatus.OK
    assert vr.repo_exists is True
    assert vr.error is None
    assert len(vr.path_results) == 2
    assert all(pr.ok for pr in vr.path_results)


# Case 2: repo 404 → REPO_MISSING.
async def test_verify_hint_repo_404_status_repo_missing(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="nonexistent-repo",
        paths=("README.md",),
    )
    script = {
        ("repos/salucallc/nonexistent-repo", None): [_FakeResp(404)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("X-99", hint)

    assert vr.status is HintStatus.REPO_MISSING
    assert vr.repo_exists is False
    assert vr.path_results == ()
    assert "404" in (vr.error or "")


# Case 3: a path in `paths` missing → PATH_MISSING.
async def test_verify_hint_missing_path_status_path_missing(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(
            "deploy/appliance/docker-compose.yml",
            "deploy/appliance/Caddyfile",
        ),
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {})],
        ("contents/deploy/appliance/docker-compose.yml", "main"): [_FakeResp(200, {})],
        ("contents/deploy/appliance/Caddyfile", "main"): [_FakeResp(404)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("OPS-03", hint)

    assert vr.status is HintStatus.PATH_MISSING
    assert vr.repo_exists is True
    # Exactly one PathResult should be non-ok.
    non_ok = [pr for pr in vr.path_results if not pr.ok]
    assert len(non_ok) == 1
    assert non_ok[0].path == "deploy/appliance/Caddyfile"
    assert non_ok[0].observed == "absent"


# Case 4: a path in `new_paths` already exists → PATH_CONFLICT.
async def test_verify_hint_new_path_conflict_status_path_conflict(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {})],
        ("contents/deploy/appliance/docker-compose.yml", "main"): [_FakeResp(200, {})],
        # IMAGE_PINS.md is meant to be new, but GitHub says it's there.
        ("contents/deploy/appliance/IMAGE_PINS.md", "main"): [_FakeResp(200, {})],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("OPS-02", hint)

    assert vr.status is HintStatus.PATH_CONFLICT
    # Conflict takes precedence over other non-OK states.
    conflicts = [pr for pr in vr.path_results
                 if pr.expected == "absent" and pr.observed == "exist"]
    assert len(conflicts) == 1


# Case 5: 5xx twice (retry exhausted) → UNVERIFIED with error.
async def test_verify_hint_5xx_twice_status_unverified(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("README.md",),
    )
    script = {
        # _gh_api retries once on 5xx — provide two 503s to exhaust.
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(503), _FakeResp(503)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("X-5XX", hint)

    # Repo probe raised (5xx twice) → UNVERIFIED, not REPO_MISSING.
    assert vr.status is HintStatus.UNVERIFIED
    assert vr.repo_exists is False
    assert "repo probe failed" in (vr.error or "")


# Case 6: 429 rate-limited → UNVERIFIED (no retry).
async def test_verify_hint_429_status_unverified(monkeypatch):
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("README.md",),
    )
    script = {
        # _gh_api on 429 raises immediately (no retry).
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(429)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("X-429", hint)

    assert vr.status is HintStatus.UNVERIFIED
    assert vr.repo_exists is False


# ── _verify_wave_hints integration ─────────────────────────────────────────


async def test_verify_wave_hints_mixed_cases(monkeypatch):
    """Three wave-1 tickets: one OK, one with unknown code (NO_HINT), one
    with a repo_missing hint. `_verify_wave_hints` should return a
    dict-by-code with all three represented."""
    orch = _mk_orchestrator()
    # OPS-01 is a real entry in _TARGET_HINTS → we script it to succeed.
    # UNKNOWN-99 is not in the table → NO_HINT, no http calls.
    # OPS-03 we override below — we can't mutate _TARGET_HINTS, so we use
    # a real code but script its repo as 404.
    t_ok = _t("u1", "SAL-1", "OPS-01", 1, "ops")
    t_no_hint = _t("u2", "SAL-2", "UNKNOWN-99", 1, "ops")
    t_missing = _t("u3", "SAL-3", "OPS-03", 1, "ops")
    _seed_graph(orch, [t_ok, t_no_hint, t_missing])

    # OPS-01 has paths=("deploy/appliance/docker-compose.yml",).
    # OPS-03 has 2 paths (Caddyfile, docker-compose.yml).
    script = {
        # OPS-01 repo + path — all happy.
        ("repos/salucallc/alfred-coo-svc", None): [
            _FakeResp(200, {"name": "alfred-coo-svc"}),
            _FakeResp(404),  # OPS-03 repo probe (simulate missing)
        ],
        ("contents/deploy/appliance/docker-compose.yml", "main"): [
            _FakeResp(200, {}),
        ],
    }
    _install_fake_client(monkeypatch, script)

    results = await orch._verify_wave_hints(1)

    assert set(results.keys()) == {"OPS-01", "UNKNOWN-99", "OPS-03"}
    assert results["OPS-01"].status is HintStatus.OK
    assert results["UNKNOWN-99"].status is HintStatus.NO_HINT
    assert results["UNKNOWN-99"].hint is None
    assert results["OPS-03"].status is HintStatus.REPO_MISSING


async def test_verify_wave_hints_no_hint_skips_http(monkeypatch):
    """A ticket whose code is not in _TARGET_HINTS must NOT make any
    GitHub call — NO_HINT is cheap by construction."""
    orch = _mk_orchestrator()
    t = _t("u1", "SAL-1", "ZZ-DOES-NOT-EXIST", 1, "other")
    _seed_graph(orch, [t])

    _install_fake_client(monkeypatch, {})  # empty script — any call explodes
    results = await orch._verify_wave_hints(1)

    assert results["ZZ-DOES-NOT-EXIST"].status is HintStatus.NO_HINT
    assert _FakeClient._calls_shared == []


async def test_verify_wave_hints_empty_code_skipped(monkeypatch):
    """A ticket with an unparseable/empty code is dropped silently —
    there's nothing to key on in _verified_hints."""
    orch = _mk_orchestrator()
    t = _t("u1", "SAL-1", "", 1, "other")
    _seed_graph(orch, [t])

    _install_fake_client(monkeypatch, {})
    results = await orch._verify_wave_hints(1)
    assert results == {}


# ── Semaphore concurrency cap ──────────────────────────────────────────────


async def test_verify_semaphore_caps_concurrency_at_8(monkeypatch):
    """Plan I §1.2: the orchestrator owns an asyncio.Semaphore(8) so a
    wave of N >> 8 tickets never fans out more than 8 hint verifications
    at a time. We count peak concurrency inside the fake response and
    assert the cap holds."""
    orch = _mk_orchestrator()
    # Build 16 tickets all pointing at OPS-01 (a real hint). We can't
    # inject 16 distinct _TARGET_HINTS entries at test time, but using the
    # same code 16× still exercises the semaphore because _verify_hint is
    # called once per ticket-code (NOT deduped at this layer).
    tickets = []
    for i in range(16):
        tickets.append(_t(f"u{i}", f"SAL-{i}", "OPS-01", 1, "ops"))
    _seed_graph(orch, tickets)

    # We need 16 repo-probe responses and 16 path-probe responses. Make
    # every probe block on an event until all 16 have entered the
    # semaphore so peak concurrency is observable.
    entered = asyncio.Event()
    count = {"n": 0}

    class _SlowResp(_FakeResp):
        async def _gate(self):
            count["n"] += 1
            if count["n"] >= 8:
                entered.set()
            await entered.wait()

    # Patch _gh_api and _gh_contents directly so we can assert semaphore
    # semantics without juggling FakeClient for 32 calls.
    original_verify = orch._verify_hint
    peak = {"n": 0, "active": 0}

    async def _instrumented_gh_api(path):
        peak["active"] += 1
        peak["n"] = max(peak["n"], peak["active"])
        await asyncio.sleep(0)  # yield
        try:
            return {"ok": True}
        finally:
            peak["active"] -= 1

    async def _instrumented_gh_contents(owner, repo, path, ref):
        peak["active"] += 1
        peak["n"] = max(peak["n"], peak["active"])
        await asyncio.sleep(0)
        try:
            return "exist"
        finally:
            peak["active"] -= 1

    monkeypatch.setattr(orch, "_gh_api", _instrumented_gh_api)
    monkeypatch.setattr(orch, "_gh_contents", _instrumented_gh_contents)

    results = await orch._verify_wave_hints(1)
    # 16 tickets, same code → last write wins in the dict, but all 16
    # _verify_hint coroutines ran.
    assert results["OPS-01"].status is HintStatus.OK
    # Semaphore(8) → peak active in-flight hint verifications ≤ 8.
    assert peak["n"] <= 8, f"peak concurrency was {peak['n']}, cap is 8"


# ── Wave-start wiring ──────────────────────────────────────────────────────


async def test_verified_hints_initialised_empty_on_construction():
    """AB-17-b instance attribute `_verified_hints` starts at {}; filled
    lazily at wave start."""
    orch = _mk_orchestrator()
    assert orch._verified_hints == {}
    # Semaphore is constructed too.
    assert isinstance(orch._verify_semaphore, asyncio.Semaphore)


async def test_verify_hint_respects_base_branch(monkeypatch):
    """Non-default base_branch must be passed through as the `ref` query
    param. Catches the easy bug where we hardcode `main`."""
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("README.md",),
        base_branch="develop",
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {})],
        ("contents/README.md", "develop"): [_FakeResp(200, {})],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("X", hint)

    assert vr.status is HintStatus.OK
    # Confirm the ref=develop param was sent.
    ref_params = [c["params"].get("ref") for c in _FakeClient._calls_shared
                  if c.get("params")]
    assert "develop" in ref_params


# ── AB-17-f · _verify_hint 8-case matrix (Plan I §5.1) ─────────────────────
#
# Plan I §1.2 aggregation axes: repo {200, 404, 5xx×2, 429} × paths × new_paths
# → {OK, PATH_MISSING, PATH_CONFLICT, UNVERIFIED, REPO_MISSING}. AB-17-b has
# partial coverage (cases 1, 2, 3, 5, 6, 8 already exist). AB-17-f adds the
# missing tiebreak + combined-mixed case + path-5xx case + "all new_paths
# absent only" and "mixed" variants to lock in the aggregation contract.
#
# Tiebreak note (Plan I §1.2, lines 2620-2640 in orchestrator.py): when BOTH
# `any_conflict_in_new_paths` and `any_missing_in_paths` fire, the aggregator
# checks conflict FIRST → status is PATH_CONFLICT. We document that here.


async def test_verify_hint_new_paths_absent_only_status_ok(monkeypatch):
    """AB-17-f · Case variant: `paths` empty, `new_paths` all absent → OK.
    Confirms the new-file-only hint shape (e.g. F08 soul-lite) aggregates
    cleanly when every new_paths probe returns 404."""
    hint = TargetHint(
        owner="salucallc",
        repo="soul-svc",
        paths=(),
        new_paths=("soul_lite/service.py", "soul_lite/routes.py"),
    )
    script = {
        ("repos/salucallc/soul-svc", None): [_FakeResp(200, {})],
        ("contents/soul_lite/service.py", "main"): [_FakeResp(404)],
        ("contents/soul_lite/routes.py", "main"): [_FakeResp(404)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("F08", hint)

    assert vr.status is HintStatus.OK
    assert vr.repo_exists is True
    assert vr.error is None
    assert len(vr.path_results) == 2
    assert all(pr.expected == "absent" and pr.observed == "absent" and pr.ok
               for pr in vr.path_results)


async def test_verify_hint_mixed_conflict_and_missing_prefers_conflict(
    monkeypatch,
):
    """AB-17-f · Case 4 (tiebreak): one `paths` entry returns 404 AND one
    `new_paths` entry returns 200. The aggregator checks conflict before
    missing (orchestrator.py §1.2 lines 2624-2630), so the locked tiebreak
    is PATH_CONFLICT."""
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {})],
        # paths entry MISSING (expected exist, observed absent).
        ("contents/deploy/appliance/docker-compose.yml", "main"): [
            _FakeResp(404),
        ],
        # new_paths entry CONFLICT (expected absent, observed exist).
        ("contents/deploy/appliance/IMAGE_PINS.md", "main"): [
            _FakeResp(200, {}),
        ],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("OPS-02", hint)

    # Tiebreak: conflict wins over missing.
    assert vr.status is HintStatus.PATH_CONFLICT
    assert vr.repo_exists is True
    # Both non-ok results present in path_results.
    non_ok = [pr for pr in vr.path_results if not pr.ok]
    assert len(non_ok) == 2
    # Error populated for non-OK.
    assert vr.error is not None and "new_paths already exist" in vr.error


async def test_verify_hint_path_5xx_twice_status_unverified(monkeypatch):
    """AB-17-f · Case 7: repo probe 200, but ONE path probe returns 5xx
    twice (retry exhausted). `_gh_contents` swallows to "unknown"; aggregator
    with `any_unknown=True` and no missing/conflict → UNVERIFIED."""
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("README.md",),
    )
    script = {
        ("repos/salucallc/alfred-coo-svc", None): [_FakeResp(200, {})],
        # _gh_contents retries once on 5xx → supply TWO 503s to exhaust.
        ("contents/README.md", "main"): [_FakeResp(503), _FakeResp(503)],
    }
    _install_fake_client(monkeypatch, script)

    orch = _mk_orchestrator()
    vr = await orch._verify_hint("X-PATH-5XX", hint)

    assert vr.status is HintStatus.UNVERIFIED
    assert vr.repo_exists is True
    assert len(vr.path_results) == 1
    assert vr.path_results[0].observed == "unknown"
    assert vr.path_results[0].ok is False
    # Error populated; points at the unknown path.
    assert vr.error is not None
    assert "unknown" in vr.error


# ── AB-17-f · _render_target_block snapshot-style variants (Plan I §3) ─────
#
# Eleven cases covering every rendering branch. Each constructs a minimal
# VerificationResult and asserts substring markers — NOT byte-for-byte
# snapshots — so AB-17-e persona vocabulary tweaks don't have to repeat
# here.


def _hint(
    *,
    owner="salucallc",
    repo="alfred-coo-svc",
    paths=("deploy/appliance/docker-compose.yml",),
    new_paths=(),
    base_branch="main",
    branch_hint="",
    notes="",
) -> TargetHint:
    return TargetHint(
        owner=owner, repo=repo, paths=paths, new_paths=new_paths,
        base_branch=base_branch, branch_hint=branch_hint, notes=notes,
    )


def test_render_target_block_ok_paths_only_has_verified_exists_marker():
    """Plan I §3 variant: OK with paths only. Every `paths` entry must
    carry the `# verified exists @ main` comment."""
    hint = _hint(paths=("deploy/appliance/docker-compose.yml",))
    vr = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=(
            PathResult(path="deploy/appliance/docker-compose.yml",
                       expected="exist", observed="exist", ok=True),
        ),
    )
    block = _render_target_block("OPS-01", vr=vr)
    assert "## Target" in block
    assert "deploy/appliance/docker-compose.yml" in block
    assert "# verified exists @ main" in block
    # No new_paths section.
    assert "new_paths:" not in block
    # No warning banner for OK.
    assert "VERIFICATION WARNING" not in block


def test_render_target_block_ok_new_paths_only_has_verified_absent_marker():
    """Plan I §3 variant: OK with new_paths only (e.g. F08). Every
    `new_paths` entry must carry `verified absent @ main — you will CREATE
    this file` and NO `paths:` section."""
    hint = _hint(
        paths=(),
        new_paths=("soul_lite/service.py",),
        repo="soul-svc",
    )
    vr = VerificationResult(
        code="F08",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=(
            PathResult(path="soul_lite/service.py",
                       expected="absent", observed="absent", ok=True),
        ),
    )
    block = _render_target_block("F08", vr=vr)
    assert "new_paths:" in block
    assert "verified absent @ main — you will CREATE this file" in block
    # Must NOT show a paths: section when hint.paths is empty.
    assert "\npaths:\n" not in block
    assert "VERIFICATION WARNING" not in block


def test_render_target_block_ok_mixed_paths_and_new_paths_markers():
    """Plan I §3 variant: OK mixed — both paths and new_paths present and
    all verified. Both marker strings must appear in the same block."""
    hint = _hint(
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    vr = VerificationResult(
        code="OPS-02",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=(
            PathResult(path="deploy/appliance/docker-compose.yml",
                       expected="exist", observed="exist", ok=True),
            PathResult(path="deploy/appliance/IMAGE_PINS.md",
                       expected="absent", observed="absent", ok=True),
        ),
    )
    block = _render_target_block("OPS-02", vr=vr)
    assert "# verified exists @ main" in block
    assert "verified absent @ main — you will CREATE this file" in block
    assert "paths:" in block
    assert "new_paths:" in block
    assert "VERIFICATION WARNING" not in block


def test_render_target_block_path_missing_shows_unresolved_and_stop():
    """Plan I §3 variant: PATH_MISSING — the missing path line must be
    `(unresolved — file ...)` + `STOP and escalate per Step 0`."""
    hint = _hint(paths=("deploy/appliance/Caddyfile",))
    vr = VerificationResult(
        code="OPS-03",
        hint=hint,
        status=HintStatus.PATH_MISSING,
        repo_exists=True,
        path_results=(
            PathResult(path="deploy/appliance/Caddyfile",
                       expected="exist", observed="absent", ok=False),
        ),
        error="one or more paths missing",
    )
    block = _render_target_block("OPS-03", vr=vr)
    assert "(unresolved — file" in block
    assert "deploy/appliance/Caddyfile" in block
    assert "STOP and escalate per Step 0" in block


def test_render_target_block_path_conflict_shows_conflict_and_already_exists():
    """Plan I §3 variant: PATH_CONFLICT — the conflicting new_paths line
    must say `(conflict — file ...)` + `already exists in`."""
    hint = _hint(
        paths=("deploy/appliance/docker-compose.yml",),
        new_paths=("deploy/appliance/IMAGE_PINS.md",),
    )
    vr = VerificationResult(
        code="OPS-02",
        hint=hint,
        status=HintStatus.PATH_CONFLICT,
        repo_exists=True,
        path_results=(
            PathResult(path="deploy/appliance/docker-compose.yml",
                       expected="exist", observed="exist", ok=True),
            PathResult(path="deploy/appliance/IMAGE_PINS.md",
                       expected="absent", observed="exist", ok=False),
        ),
        error="one or more new_paths already exist",
    )
    block = _render_target_block("OPS-02", vr=vr)
    assert "(conflict — file" in block
    assert "already exists in" in block
    assert "deploy/appliance/IMAGE_PINS.md" in block


def test_render_target_block_unverified_prepends_warning_banner():
    """Plan I §3 variant: UNVERIFIED — the block must be prefixed with a
    `# VERIFICATION WARNING:` banner AND every `unknown` path line must
    show `(unverified — <error>)` with the vr.error string."""
    hint = _hint(paths=("README.md",))
    vr = VerificationResult(
        code="X",
        hint=hint,
        status=HintStatus.UNVERIFIED,
        repo_exists=True,
        path_results=(
            PathResult(path="README.md", expected="exist",
                       observed="unknown", ok=False),
        ),
        error="one or more paths returned unknown status",
    )
    block = _render_target_block("X", vr=vr)
    assert "# VERIFICATION WARNING:" in block
    # Banner must precede the `## Target` header.
    assert block.index("VERIFICATION WARNING") < block.index("## Target")
    # Unknown-path line carries the error string.
    assert "(unverified — one or more paths returned unknown status" in block


def test_render_target_block_no_hint_has_unresolved_with_code():
    """Plan I §3 variant: NO_HINT — render the `(unresolved — no hint for
    code X)` escalation block. AB-17-c enriched the legacy string with the
    triggering code."""
    vr = VerificationResult(
        code="ZZZ-99",
        hint=None,
        status=HintStatus.NO_HINT,
        repo_exists=False,
        path_results=(),
        error="no hint for code ZZZ-99",
    )
    block = _render_target_block("ZZZ-99", vr=vr)
    assert "(unresolved — no hint for code ZZZ-99" in block
    assert "linear_create_issue" in block
    assert "STOP" in block


def test_render_target_block_repo_missing_defensive_fallback():
    """Plan I §3 variant: REPO_MISSING (defensive) — AB-17-d is supposed
    to skip dispatch for REPO_MISSING, so this branch should be unreachable
    in prod. The renderer still emits `(blocked — repo ...)` +
    `dispatch should not have happened` so any misuse is visible."""
    hint = _hint(repo="nonexistent-repo")
    vr = VerificationResult(
        code="X-99",
        hint=hint,
        status=HintStatus.REPO_MISSING,
        repo_exists=False,
        path_results=(),
        error="repo salucallc/nonexistent-repo returned 404 at ref main",
    )
    block = _render_target_block("X-99", vr=vr)
    assert "(blocked — repo" in block
    assert "salucallc/nonexistent-repo" in block
    assert "dispatch should not have happened" in block


def test_render_target_block_vr_none_known_code_matches_legacy_format():
    """Plan I §3 variant: vr=None with known code — must render the
    pre-AB-17 legacy block byte-for-byte (static `_TARGET_HINTS` lookup,
    no verification comments). Locked so AB-17-c back-compat snapshot
    tests keep passing."""
    block = _render_target_block("OPS-01", vr=None)
    # Legacy shape: owner/repo/paths/base_branch, no verification markers.
    assert "## Target\n" in block
    assert "owner: salucallc" in block
    assert "repo:  alfred-coo-svc" in block
    assert "paths:" in block
    assert "base_branch: main" in block
    # Absolutely NO verification-mode markers.
    assert "# verified" not in block
    assert "VERIFICATION WARNING" not in block
    assert "(unresolved" not in block
    assert "(conflict" not in block
    assert "(unverified" not in block


def test_render_target_block_vr_none_unknown_code_matches_legacy_unresolved():
    """Plan I §3 variant: vr=None with unknown code — must render the
    pre-AB-17 legacy `(unresolved — consult plan doc; STOP and escalate
    via linear_create_issue per Step 0 of your persona protocol)` block.
    Byte-for-byte preservation prevents snapshot-test drift."""
    block = _render_target_block("NOPE-404", vr=None)
    expected = (
        "## Target\n"
        "(unresolved — consult plan doc; STOP and escalate via "
        "linear_create_issue per Step 0 of your persona protocol)\n"
    )
    assert block == expected


# ── AB-17-f · _mark_repo_missing_tickets integration (Plan I §1.4 + §2.3) ──


async def test_mark_repo_missing_tickets_blocks_only_repo_missing(
    monkeypatch, caplog,
):
    """AB-17-d wiring: `_mark_repo_missing_tickets` must (a) emit a
    grounding-gap Linear issue exactly once for each REPO_MISSING ticket,
    (b) mark only those tickets FAILED, (c) record their ids in
    `_repo_missing_tickets`, (d) NOT touch tickets with OK status,
    (e) NOT call _update_linear_state (parent stays Backlog per §5.1 R-d),
    (f) dedupe via `_emitted_blocks` on a second invocation."""
    orch = _mk_orchestrator()

    # Two tickets — OPS-01 (OK) and OPS-03 (REPO_MISSING).
    t_ok = _t("u-ok", "SAL-1001", "OPS-01", 1, "ops")
    t_blocked = _t("u-blocked", "SAL-1002", "OPS-03", 1, "ops")
    _seed_graph(orch, [t_ok, t_blocked])

    # Pre-populate _verified_hints to simulate wave-start verification.
    ok_hint = _TARGET_HINTS["OPS-01"]
    blocked_hint = TargetHint(
        owner="salucallc",
        repo="phantom-repo",
        paths=("README.md",),
    )
    orch._verified_hints = {
        "OPS-01": VerificationResult(
            code="OPS-01", hint=ok_hint, status=HintStatus.OK,
            repo_exists=True,
            path_results=(
                PathResult(path="deploy/appliance/docker-compose.yml",
                           expected="exist", observed="exist", ok=True),
            ),
        ),
        "OPS-03": VerificationResult(
            code="OPS-03", hint=blocked_hint, status=HintStatus.REPO_MISSING,
            repo_exists=False, path_results=(),
            error="repo salucallc/phantom-repo returned 404 at ref main",
        ),
    }

    # Mock linear_create_issue via BUILTIN_TOOLS.
    from alfred_coo import tools as _tools_mod
    calls: list[dict] = []

    async def _fake_linear_create(
        title, description="", priority=3, due_date=None, labels=None,
    ):
        calls.append({
            "title": title, "description": description,
            "priority": priority, "labels": labels,
        })
        return {"identifier": "SAL-9999", "url": "https://linear.app/x"}

    original_spec = _tools_mod.BUILTIN_TOOLS["linear_create_issue"]
    # Substitute the handler — keep everything else intact.
    from alfred_coo.tools import ToolSpec
    fake_spec = ToolSpec(
        name=original_spec.name,
        description=original_spec.description,
        parameters=original_spec.parameters,
        handler=_fake_linear_create,
    )
    monkeypatch.setitem(
        _tools_mod.BUILTIN_TOOLS, "linear_create_issue", fake_spec,
    )

    # Also guard against accidental _update_linear_state by patching it to
    # raise. If _mark_repo_missing_tickets ever calls it we fail loudly.
    async def _boom(*a, **kw):
        raise AssertionError(
            "_update_linear_state must NOT be called by "
            "_mark_repo_missing_tickets (parent ticket stays Backlog)"
        )
    monkeypatch.setattr(orch, "_update_linear_state", _boom, raising=False)

    import logging as _logging
    with caplog.at_level(_logging.WARNING,
                         logger="alfred_coo.autonomous_build.orchestrator"):
        await orch._mark_repo_missing_tickets([t_ok, t_blocked])

    # (a) one grounding-gap issue emitted.
    assert len(calls) == 1
    assert "[grounding-gap] BLOCKED" in calls[0]["title"]
    assert "SAL-1002" in calls[0]["title"]
    # labels forward-compat marker.
    assert "grounding-gap" in (calls[0]["labels"] or [])

    # (b) FAILED applied only to blocked ticket.
    assert t_blocked.status is TicketStatus.FAILED
    assert t_ok.status is TicketStatus.PENDING  # unchanged

    # (c) `_repo_missing_tickets` contains the blocked ticket UUID, NOT OK.
    assert t_blocked.id in orch._repo_missing_tickets
    assert t_ok.id not in orch._repo_missing_tickets

    # (d) WARN log line emitted with reason=repo_missing.
    warn_lines = [r for r in caplog.records
                  if r.levelname == "WARNING"
                  and "reason=repo_missing" in r.getMessage()]
    assert len(warn_lines) == 1, (
        f"expected exactly one reason=repo_missing warning, got "
        f"{[r.getMessage() for r in warn_lines]}"
    )

    # (f) dedupe: second invocation must NOT re-emit (via _emitted_blocks).
    await orch._mark_repo_missing_tickets([t_ok, t_blocked])
    assert len(calls) == 1, (
        "linear_create_issue should NOT be re-called on second invocation "
        f"(dedupe via _emitted_blocks), got {len(calls)} total calls"
    )
    # Blocked ticket UUID still recorded.
    assert t_blocked.id in orch._repo_missing_tickets


async def test_mark_repo_missing_tickets_noop_when_verified_hints_empty():
    """AB-17-d guard: if verification was skipped (empty `_verified_hints`)
    we must NOT treat any ticket as blocked. Plan I §1.4: dispatch as
    today rather than mass-blocking on a verifier crash."""
    orch = _mk_orchestrator()
    t = _t("u-ok", "SAL-1", "OPS-01", 1, "ops")
    _seed_graph(orch, [t])

    assert orch._verified_hints == {}
    await orch._mark_repo_missing_tickets([t])

    # Nothing changed.
    assert t.status is TicketStatus.PENDING
    assert orch._repo_missing_tickets == set()
    assert orch._emitted_blocks == set()


async def test_child_task_body_uses_verified_hints_without_hasattr_guard(
    monkeypatch,
):
    """AB-17-f init tweak: `_verified_hints` is always present on the
    orchestrator (initialized in __init__), so `_child_task_body` passes
    the verification result through without needing a `hasattr` guard.
    This test pre-populates _verified_hints and asserts the rendered
    block shows the verified marker."""
    orch = _mk_orchestrator()
    # Sanity: attribute is always present on freshly-constructed orchestrator.
    assert hasattr(orch, "_verified_hints")
    assert orch._verified_hints == {}

    # Build an OK verification result for OPS-01.
    hint = _TARGET_HINTS["OPS-01"]
    orch._verified_hints["OPS-01"] = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ),
    )
    ticket = _t("u-ops-01", "SAL-2634", "OPS-01", 0, "ops", size="S")
    body = orch._child_task_body(ticket)

    # When vr is present, the verified-exists marker appears.
    assert "# verified exists @ main" in body
    # And no legacy fallthrough to `(unresolved)`.
    assert "(unresolved" not in body


# ── AB-17-k · respawn grounding + verdict extraction hardening ─────────────
#
# v8-smoke-e (mesh task c4459e37, 2026-04-24 ~16:33 UTC) passed the human
# gate but failed the orchestrator's internal gate at green_ratio=0.87.
# Three distinct defects diagnosed by debug sub ad3aa6937:
#   1. `_respawn_child_with_fixes` omitted the ## Target block the initial
#      dispatch renders (SAL-2634 fix-round silent-escalated).
#   2. `_VERDICT_REQUEST_CHANGES_RE` missed past-tense "Requested changes"
#      (SAL-2583 silent verdict, trace row 115).
#   3. `_extract_verdict` priority-1 read `result.state` but the mesh-task
#      daemon persists tool-call *arguments*, not *results*; priority-1b
#      now inspects arguments directly.


def test_respawn_body_includes_target_block():
    """AB-17-k · Edit 1 · `_respawn_child_with_fixes` body now renders the
    same `## Target` block as `_child_task_body`. v8-smoke-e SAL-2634:
    the respawned child had no target grounding, silent-escalated via
    linear_create_issue against a broken prompt. Regression guard.
    """
    orch = _mk_orchestrator()
    # Pre-populate _verified_hints so the rendered block carries the
    # owner/repo + verified-exists marker the child would have seen on
    # initial dispatch.
    hint = _TARGET_HINTS["OPS-01"]
    orch._verified_hints["OPS-01"] = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ),
    )
    ticket = _t("u-ops-01", "SAL-2634", "OPS-01", 0, "ops", size="S")
    ticket.pr_url = "https://github.com/salucallc/alfred-coo-svc/pull/42"
    ticket.review_cycles = 1

    asyncio.run(orch._respawn_child_with_fixes(ticket, "please address foo"))

    # The mesh_create_task call captured the body we care about.
    assert orch.mesh.created, "respawn should have created a child task"
    body = orch.mesh.created[-1]["description"]
    assert "## Target" in body
    # Owner/repo pinned from the verified hint.
    assert hint.owner in body
    assert hint.repo in body
    # And the verified-exists marker from _render_target_block.
    assert "# verified exists @ main" in body


def test_verdict_regex_matches_past_tense():
    """AB-17-k · Edit 2 · `_VERDICT_REQUEST_CHANGES_RE` now matches
    past-tense + gerund variants. v8-smoke-e SAL-2583 trace row 115:
    envelope summary was "Requested changes", which the AB-17-i pattern
    missed (only `request(?:ing)?`). Regression guard.
    """
    accept = [
        "REQUEST_CHANGES",
        "request_changes",
        "request changes",
        "Requesting changes",
        "Requested changes",
        "Request Change",
    ]
    for s in accept:
        assert _VERDICT_REQUEST_CHANGES_RE.search(s), f"should match: {s!r}"

    # Reject: unrelated vocabulary must not match.
    assert not _VERDICT_REQUEST_CHANGES_RE.search("disapprove")


def test_extract_verdict_from_tool_call_args():
    """AB-17-k · Edit 3 · `_extract_verdict` priority-1b inspects tool-call
    arguments when mesh doesn't persist results. Synthetic `pr_review`
    call with arguments={"event":"REQUEST_CHANGES"} + empty result.
    Asserts verdict is extracted via the new priority-1b path, before
    falling through to the priority-2 summary regex.
    """
    result = {
        "tool_calls": [
            {
                "name": "pr_review",
                # Empty result/output mirrors the mesh-task daemon shape
                # observed in v8-smoke-e SAL-2583.
                "result": {},
                "arguments": {
                    "event": "REQUEST_CHANGES",
                    "body": "address the target grounding gap",
                },
            }
        ],
        # Summary contains no verdict token, so priority-2 regex must
        # fail — if the test passes, priority-1b is the only source.
        "summary": "review completed",
    }
    assert AutonomousBuildOrchestrator._extract_verdict(result) == "REQUEST_CHANGES"

    # Also cover the JSON-string arguments shape some mesh adapters use.
    result_json_args = {
        "tool_calls": [
            {
                "name": "pr_review",
                "result": None,
                "arguments": json.dumps({"event": "APPROVE"}),
            }
        ],
        "summary": "",
    }
    assert AutonomousBuildOrchestrator._extract_verdict(result_json_args) == "APPROVE"


# ── AB-17-n: wave-dispatch deadlock detector ───────────────────────────────


async def test_dispatch_wave_deadlock_detected_and_broken(monkeypatch, caplog):
    """AB-17-n regression: when wave-0 children fail without PRs and
    downstream tickets are BLOCKED on those FAILED upstreams, the
    `_dispatch_wave` loop used to spin forever because BLOCKED is not in
    TERMINAL_STATES and `_deps_satisfied` permanently returned False.
    The detector must coerce the BLOCKED tickets to FAILED, emit
    `ticket_forced_failed_deadlock` events, and exit the loop.
    Observed in v8-full (mesh task e7f85521) + v8-full-v2 (6fdf760f)
    2026-04-24; debug sub af2c179d.
    """
    import logging

    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0

    # T1 already FAILED (wave-0 child died without PR). T2 and T3 have
    # blocks_in=[T1.id] so they will flip PENDING -> BLOCKED on the very
    # first `_select_ready` tick and remain stuck forever absent the
    # detector.
    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t2 = _t("u2", "SAL-2", "TIR-02", 0, "tiresias", blocks_in=["u1"])
    t3 = _t("u3", "SAL-3", "TIR-03", 0, "tiresias", blocks_in=["u1"])
    t1.status = TicketStatus.FAILED
    _seed_graph(orch, [t1, t2, t3])

    # Stub every side-effect the dispatch loop performs so the test
    # isolates the deadlock detector. Any of these failing in production
    # would be surfaced by its own test; here we just need no-ops.
    async def _noop(*args, **kwargs):
        return None
    async def _noop_list(*args, **kwargs):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_children", _noop_list)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    # Replace the module-level checkpoint helper so we don't touch soul.
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )

    # Count ticks so we can assert bounded termination (no infinite loop).
    ticks = {"n": 0}
    real_sleep = asyncio.sleep
    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] > 10:
            raise RuntimeError(
                "deadlock detector failed to break loop within 10 ticks"
            )
        await real_sleep(0)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    with caplog.at_level(logging.ERROR, logger="alfred_coo.autonomous_build.orchestrator"):
        await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    # T2 + T3 should both be coerced to FAILED by the detector.
    assert t2.status == TicketStatus.FAILED, (
        f"expected T2 FAILED, got {t2.status}"
    )
    assert t3.status == TicketStatus.FAILED, (
        f"expected T3 FAILED, got {t3.status}"
    )
    # T1 stays FAILED (precondition).
    assert t1.status == TicketStatus.FAILED

    # record_event fired once per coerced ticket with the upstream failure
    # list populated.
    forced = [
        e for e in orch.state.events
        if e["kind"] == "ticket_forced_failed_deadlock"
    ]
    assert len(forced) == 2, (
        f"expected 2 ticket_forced_failed_deadlock events, got {len(forced)}: "
        f"{forced}"
    )
    identifiers = {e["identifier"] for e in forced}
    assert identifiers == {"SAL-2", "SAL-3"}
    # Each coerced ticket cites T1 as the upstream failure.
    for e in forced:
        assert e["upstream_failed"] == ["SAL-1"], (
            f"expected upstream_failed=['SAL-1'], got {e['upstream_failed']}"
        )

    # ERROR log mentions "deadlock".
    deadlock_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.ERROR and "deadlock" in r.getMessage()
    ]
    assert deadlock_msgs, (
        f"expected an ERROR log containing 'deadlock'; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # Loop exited within a handful of ticks (the detector breaks
    # immediately when it fires, so ticks["n"] should be small).
    assert ticks["n"] <= 3, (
        f"detector took {ticks['n']} ticks to break; expected <=3"
    )


# ── AB-17-o · Prior PR block + update_pr wiring ────────────────────────────
#
# v8-full-v4 (mesh task 83dd216d, 2026-04-24 ~18:14 UTC) exposed the
# duplicate-PR leak. Respawns were correctly firing after AB-17-k but
# calling ``propose_pr`` with a fresh branch, opening a new PR per cycle
# (acs#59/60, ts#4/5, ss#17/18). AB-17-o: orchestrator now renders a
# ``## Prior PR`` section with the existing URL + branch, and the
# alfred-coo-a persona prompt tells the builder to call ``update_pr``
# against it instead of ``propose_pr``.


def test_respawn_body_includes_prior_pr_section(monkeypatch):
    """Edit 2 · `_respawn_child_with_fixes` injects `## Prior PR` naming
    the existing PR URL + branch. Branch is resolved via `_gh_api`; we
    stub that to return head.ref='feature/sal-2634-existing' so the
    assertion can pin the exact string the builder will read.
    """
    orch = _mk_orchestrator()
    hint = _TARGET_HINTS["OPS-01"]
    orch._verified_hints["OPS-01"] = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ),
    )
    ticket = _t("u-ops-01", "SAL-2634", "OPS-01", 0, "ops", size="S")
    ticket.pr_url = "https://github.com/salucallc/alfred-coo-svc/pull/42"
    ticket.review_cycles = 1

    # Stub _gh_api so _lookup_pr_branch resolves to a deterministic branch.
    async def _fake_gh_api(path):
        assert path == "repos/salucallc/alfred-coo-svc/pulls/42"
        return {"head": {"ref": "feature/sal-2634-existing"}}
    orch._gh_api = _fake_gh_api  # type: ignore[assignment]

    asyncio.run(orch._respawn_child_with_fixes(ticket, "address foo"))

    assert orch.mesh.created, "respawn should have created a child task"
    body = orch.mesh.created[-1]["description"]
    assert "## Prior PR" in body
    assert "url: https://github.com/salucallc/alfred-coo-svc/pull/42" in body
    assert "branch: feature/sal-2634-existing" in body
    # And the steering sentence that tells the builder to pick update_pr.
    assert "update_pr" in body
    # Existing AB-17-k contract still holds: Target block rendered too.
    assert "## Target" in body


def test_respawn_body_prior_pr_lookup_failure_emits_placeholder():
    """If `_gh_api` returns None (404 or transport fail), the Prior PR
    block still renders with a `(lookup failed ...)` marker so the child
    surfaces it as a grounding gap rather than silently opening a new PR.
    """
    orch = _mk_orchestrator()
    hint = _TARGET_HINTS["OPS-01"]
    orch._verified_hints["OPS-01"] = VerificationResult(
        code="OPS-01",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ),
    )
    ticket = _t("u-ops-01", "SAL-2634", "OPS-01", 0, "ops", size="S")
    ticket.pr_url = "https://github.com/salucallc/alfred-coo-svc/pull/42"
    ticket.review_cycles = 1

    async def _fake_gh_api(path):
        return None  # simulate 404
    orch._gh_api = _fake_gh_api  # type: ignore[assignment]

    asyncio.run(orch._respawn_child_with_fixes(ticket, "address foo"))

    body = orch.mesh.created[-1]["description"]
    assert "## Prior PR" in body
    assert "lookup failed" in body
    # Still tells the child not to call propose_pr.
    assert "update_pr" in body


def test_alfred_coo_a_persona_mentions_update_pr_for_fix_round():
    """Edit 3 · alfred-coo-a Step 6 addendum mentions `update_pr` and
    the `## Prior PR` section so a fix-round child picks the right tool.
    """
    from alfred_coo.persona import BUILTIN_PERSONAS
    prompt = BUILTIN_PERSONAS["alfred-coo-a"].system_prompt
    assert "update_pr" in prompt, (
        "alfred-coo-a prompt must mention update_pr for fix-round variant"
    )
    assert "Prior PR" in prompt, (
        "alfred-coo-a prompt must reference the `## Prior PR` section"
    )
    # And update_pr is in the tool allowlist.
    tools = BUILTIN_PERSONAS["alfred-coo-a"].tools
    assert "update_pr" in tools, (
        f"update_pr missing from alfred-coo-a tool allowlist: {tools}"
    )


# ── AB-17-p · per-tick liveness + no-forward-progress watchdog ────────────


async def test_dispatch_wave_emits_no_progress_warning(monkeypatch, caplog):
    """AB-17-p: when the wave has in-flight work but `_last_progress_ts`
    is older than PROGRESS_STALL_WARN_SEC, `_dispatch_wave` emits a
    `[watchdog] wave N no forward progress` WARN log and a
    `wave_no_progress` state event. Visibility ONLY — the loop does NOT
    cancel, retry, or mark tickets failed. The deadlock path (AB-17-n)
    is separately responsible for terminal structural issues.
    """
    import logging
    from alfred_coo.autonomous_build.orchestrator import (
        PROGRESS_STALL_WARN_SEC,
    )

    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0

    # One DISPATCHED ticket so `_in_flight_for_wave` is non-empty.
    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t1.status = TicketStatus.DISPATCHED
    t1.child_task_id = "mesh-1"
    _seed_graph(orch, [t1])

    # Age _last_progress_ts past the threshold so the watchdog fires on
    # the very first tick.
    orch._last_progress_ts = (
        __import__("time").time() - (PROGRESS_STALL_WARN_SEC + 30)
    )

    async def _noop(*args, **kwargs):
        return None
    async def _noop_list(*args, **kwargs):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_children", _noop_list)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )

    # Bail out of the while-loop after one tick by flipping the ticket
    # terminal in `asyncio.sleep` — the watchdog runs BEFORE the sleep,
    # so by the time we flip the status the WARN + event are already
    # emitted.
    async def _flip_then_sleep(delay):
        t1.status = TicketStatus.MERGED_GREEN
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        _flip_then_sleep,
    )

    with caplog.at_level(
        logging.WARNING, logger="alfred_coo.autonomous_build.orchestrator"
    ):
        await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    # WARN log fired.
    watchdog_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING
        and "[watchdog]" in r.getMessage()
        and "no forward progress" in r.getMessage()
    ]
    assert watchdog_msgs, (
        f"expected a watchdog WARN log; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # `wave_no_progress` state event recorded.
    events = [e for e in orch.state.events if e["kind"] == "wave_no_progress"]
    assert len(events) == 1, (
        f"expected exactly one wave_no_progress event, got {len(events)}: "
        f"{events}"
    )
    evt = events[0]
    assert evt["wave"] == 0
    assert evt["in_flight"] == 1
    assert evt["ready"] == 0
    assert evt["stall_sec"] >= PROGRESS_STALL_WARN_SEC

    # Ticket was NOT mutated by the watchdog (visibility only).
    # It flipped to MERGED_GREEN inside `_flip_then_sleep`, but not FAILED.
    assert t1.status == TicketStatus.MERGED_GREEN


async def test_dispatch_wave_watchdog_silent_when_no_in_flight(monkeypatch, caplog):
    """AB-17-p: the watchdog guard requires in-flight work. A wave
    with nothing dispatched yet (pre-dispatch tick) must NOT emit the
    warning even if `_last_progress_ts` is stale — that's the expected
    idle state, not a stall.
    """
    import logging
    from alfred_coo.autonomous_build.orchestrator import (
        PROGRESS_STALL_WARN_SEC,
    )

    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0

    # PENDING, never dispatched.
    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    _seed_graph(orch, [t1])

    orch._last_progress_ts = (
        __import__("time").time() - (PROGRESS_STALL_WARN_SEC + 30)
    )

    async def _noop(*args, **kwargs):
        return None
    async def _noop_list(*args, **kwargs):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_children", _noop_list)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    # Stub dispatch so the tick completes without real mesh traffic.
    async def _fake_dispatch(ticket):
        ticket.status = TicketStatus.MERGED_GREEN
    monkeypatch.setattr(orch, "_dispatch_child", _fake_dispatch)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )
    async def _real_sleep(delay):
        pass
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        _real_sleep,
    )

    with caplog.at_level(
        logging.WARNING, logger="alfred_coo.autonomous_build.orchestrator"
    ):
        await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    # No watchdog WARN should have fired (in_flight was [] at tick-top).
    watchdog_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING
        and "[watchdog]" in r.getMessage()
    ]
    assert not watchdog_msgs, (
        f"watchdog should be silent with no in-flight; got: {watchdog_msgs}"
    )
    # And no state event.
    events = [e for e in orch.state.events if e["kind"] == "wave_no_progress"]
    assert not events, f"unexpected wave_no_progress events: {events}"


# ── SAL-2787 · per-dispatch hint re-verify (cache-staleness race fix) ──────


async def test_dispatch_child_refreshes_verified_hints_cache(monkeypatch):
    """SAL-2787: ``_dispatch_child`` must call ``_verify_hint`` and
    overwrite ``self._verified_hints[code]`` BEFORE building the task body,
    so a stale wave-start cache entry (e.g. ``OK`` while a sibling builder
    has since merged the ``new_paths`` file) cannot escape into the child.

    Audit ref: ``Z:/_planning/v1-ga/hints_audit_2026-04-24.md``; v7e wave 0
    (2026-04-24) produced 6 dispatches → 0 PRs because every child correctly
    grounded out on a STEP-2 re-verify that flipped the cached OK to
    PATH_CONFLICT.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    # Pre-populate _verified_hints with a stale OK for OPS-02 — simulating
    # wave-start verification that happened BEFORE a sibling merged
    # IMAGE_PINS.md.
    hint = _TARGET_HINTS["OPS-02"]
    stale_vr = VerificationResult(
        code="OPS-02",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ) + tuple(
            PathResult(path=p, expected="absent", observed="absent", ok=True)
            for p in hint.new_paths
        ),
    )
    orch._verified_hints["OPS-02"] = stale_vr

    # Patch _verify_hint to return the *fresh* state (path_conflict — the
    # IMAGE_PINS.md file is now present on main).
    fresh_vr = VerificationResult(
        code="OPS-02",
        hint=hint,
        status=HintStatus.PATH_CONFLICT,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ) + tuple(
            PathResult(path=p, expected="absent", observed="exist", ok=False)
            for p in hint.new_paths
        ),
        error="one or more new_paths already exist",
    )
    verify_calls: list[tuple[str, str]] = []

    async def _fake_verify_hint(code, h):
        verify_calls.append((code, h.repo))
        return fresh_vr
    monkeypatch.setattr(orch, "_verify_hint", _fake_verify_hint)

    # Stub Linear (not under test).
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    ticket = _t("u-ops-02", "SAL-2700", "OPS-02", 0, "ops", size="S")
    await orch._dispatch_child(ticket)

    # Re-verify was called exactly once for this ticket's code.
    assert verify_calls == [("OPS-02", hint.repo)], (
        f"expected one _verify_hint call for OPS-02, got: {verify_calls}"
    )
    # Cache now reflects the fresh path_conflict, NOT the stale OK.
    assert orch._verified_hints["OPS-02"] is fresh_vr
    assert orch._verified_hints["OPS-02"].status is HintStatus.PATH_CONFLICT


async def test_dispatch_child_verify_handles_unhinted_ticket(monkeypatch):
    """SAL-2787: a ticket whose code is NOT in ``_TARGET_HINTS`` must NOT
    trigger ``_verify_hint`` (no hint → nothing to verify) and must not
    raise. The unresolved-render path in ``_child_task_body`` handles it.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    verify_calls: list[str] = []

    async def _exploding_verify_hint(code, h):
        verify_calls.append(code)
        raise AssertionError(
            "_verify_hint must NOT be called for unhinted tickets"
        )
    monkeypatch.setattr(orch, "_verify_hint", _exploding_verify_hint)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    # Code not in _TARGET_HINTS.
    assert "NOT-A-REAL-CODE-99" not in _TARGET_HINTS
    ticket = _t("u-x", "SAL-9999", "NOT-A-REAL-CODE-99", 0, "ops", size="S")

    # Must not raise.
    await orch._dispatch_child(ticket)

    assert verify_calls == [], (
        f"unhinted ticket triggered verify: {verify_calls}"
    )
    # Empty-code ticket too: no verify, no error.
    ticket2 = _t("u-y", "SAL-9998", "", 0, "ops", size="S")
    await orch._dispatch_child(ticket2)
    assert verify_calls == []


async def test_dispatch_child_uses_fresh_hint_in_body(monkeypatch):
    """SAL-2787: end-to-end — when ``_verify_hint`` returns ``PATH_CONFLICT``
    mid-test, the rendered ``## Target`` block in the dispatched body must
    carry the conflict marker (``# CONFLICT: file already exists ...``)
    rather than the stale ``# verified absent ...`` from the wave-start
    cache. Asserts the fresh state actually reaches the child.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    # Stale OK in cache.
    hint = _TARGET_HINTS["OPS-02"]
    orch._verified_hints["OPS-02"] = VerificationResult(
        code="OPS-02",
        hint=hint,
        status=HintStatus.OK,
        repo_exists=True,
        path_results=tuple(
            PathResult(path=p, expected="exist", observed="exist", ok=True)
            for p in hint.paths
        ) + tuple(
            PathResult(path=p, expected="absent", observed="absent", ok=True)
            for p in hint.new_paths
        ),
    )

    # Patch _verify_hint to flip to PATH_CONFLICT.
    async def _fake_verify_hint(code, h):
        return VerificationResult(
            code=code,
            hint=h,
            status=HintStatus.PATH_CONFLICT,
            repo_exists=True,
            path_results=tuple(
                PathResult(path=p, expected="exist", observed="exist", ok=True)
                for p in h.paths
            ) + tuple(
                PathResult(path=p, expected="absent", observed="exist", ok=False)
                for p in h.new_paths
            ),
            error="one or more new_paths already exist",
        )
    monkeypatch.setattr(orch, "_verify_hint", _fake_verify_hint)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    ticket = _t("u-ops-02", "SAL-2700", "OPS-02", 0, "ops", size="S")
    await orch._dispatch_child(ticket)

    # The mesh recorded the dispatched body — assert the conflict marker
    # made it in (i.e. the fresh re-verify drove the render, not the
    # stale OK).
    assert mesh.created, "expected a mesh task to be created"
    body = mesh.created[-1]["description"]
    assert "## Target" in body
    # _render_target_block emits a "(conflict — file ... already exists"
    # marker on each `new_paths` entry whose observed flipped to "exist".
    assert "(conflict —" in body, (
        f"expected conflict marker in fresh-rendered Target block; got:\n{body}"
    )
    # And conversely, the rendered block must NOT carry the stale
    # "verified absent" marker that the cached OK would have produced.
    assert "# verified absent @" not in body, (
        f"stale 'verified absent' marker leaked through; body:\n{body}"
    )


# ── AB-17-q · external cancel signal (SAL-2756) ────────────────────────────


class _FakeMeshWithCancelSignal(_FakeMesh):
    """_FakeMesh extension that returns a `failed` record for the kickoff
    task after N polls of `get_task`. Used to simulate an operator
    PATCHing the kickoff to canceled mid-run.
    """

    def __init__(self, kickoff_id: str, cancel_after_polls: int,
                 cancel_result: dict | None = None):
        super().__init__()
        self.kickoff_id = kickoff_id
        self.cancel_after_polls = cancel_after_polls
        self.cancel_result = cancel_result or {"cancel": True, "reason": "manual_test"}
        self._get_task_calls = 0

    async def get_task(self, task_id: str):
        self._get_task_calls += 1
        if task_id == self.kickoff_id and self._get_task_calls > self.cancel_after_polls:
            return {
                "id": self.kickoff_id,
                "status": "failed",
                "result": self.cancel_result,
            }
        return None


async def test_orchestrator_external_cancel_drains_and_exits(monkeypatch, caplog):
    """AB-17-q (SAL-2756) regression: when the kickoff task is PATCHed
    to status=failed with result.cancel=true mid-wave, the orchestrator
    observes the signal at the next dispatch tick, sets `_drain_mode`
    so no new children dispatch, lets in-flight children complete, and
    exits the wave loop without raising.

    Setup: 4 wave-0 tickets, max_parallel_subs=2.
      - tick 1: dispatches 2 children (T1, T2), poll completes T1
      - tick 2: cancel observed, drain — T2 still in-flight
      - tick 3: poll completes T2; in-flight empty; cancel exit fires

    Asserts:
      - T3, T4 NEVER dispatched (proves drain-mode skipped new children)
      - T1, T2 reach MERGED_GREEN (proves in-flight allowed to finish)
      - state events include `cancel_requested` + `wave_dispatch_canceled`
      - mesh.complete called once with status="failed" + cancel=True
    """
    import logging

    kickoff_id = "kick-cancel-test"
    mesh = _FakeMeshWithCancelSignal(
        kickoff_id=kickoff_id,
        cancel_after_polls=1,  # cancel observed on the 2nd poll
    )
    orch = AutonomousBuildOrchestrator(
        task={"id": kickoff_id, "title": "[persona:autonomous-build-a] kickoff",
              "description": ""},
        persona=_mk_persona(),
        mesh=mesh,
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    orch.poll_sleep_sec = 0
    orch.max_parallel_subs = 2  # constrains tick-1 to 2 dispatches

    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t2 = _t("u2", "SAL-2", "TIR-02", 0, "tiresias")
    t3 = _t("u3", "SAL-3", "TIR-03", 0, "tiresias")
    t4 = _t("u4", "SAL-4", "TIR-04", 0, "tiresias")
    _seed_graph(orch, [t1, t2, t3, t4])

    # Stub side-effects we don't care about.
    async def _noop(*a, **kw):
        return None
    async def _noop_list(*a, **kw):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )

    # Track which tickets have been dispatched as children so we can
    # advance their state on subsequent ticks. The fake `_dispatch_child`
    # records the child_task_id and flips status to DISPATCHED.
    dispatched_order: list[Ticket] = []
    async def _fake_dispatch_child(ticket):
        dispatched_order.append(ticket)
        ticket.child_task_id = f"child-{ticket.identifier}"
        ticket.status = TicketStatus.DISPATCHED
        # Record on mesh.created so the test can assert dispatch counts.
        mesh.created.append({
            "title": ticket.identifier,
            "description": "",
            "from_session_id": None,
        })
    monkeypatch.setattr(orch, "_dispatch_child", _fake_dispatch_child)

    # `_poll_children` advances dispatched tickets one step per tick:
    # tick 1 (post-dispatch): T1 -> MERGED_GREEN
    # tick 2 (post-cancel):   T2 -> MERGED_GREEN
    poll_call = {"n": 0}
    async def _fake_poll_children():
        poll_call["n"] += 1
        # Pick the FIRST non-terminal dispatched ticket and complete it.
        for t in dispatched_order:
            if t.status not in TERMINAL_STATES:
                t.status = TicketStatus.MERGED_GREEN
                return [t]
        return []
    monkeypatch.setattr(orch, "_poll_children", _fake_poll_children)

    # Bound the loop in case of a regression.
    ticks = {"n": 0}
    real_sleep = asyncio.sleep
    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] > 20:
            raise RuntimeError(
                "cancel exit failed to break dispatch loop within 20 ticks"
            )
        await real_sleep(0)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    with caplog.at_level(logging.WARNING, logger="alfred_coo.autonomous_build.orchestrator"):
        await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    # ── core assertions ────────────────────────────────────────────────────

    # Cancel was observed and drain mode flipped on.
    assert orch._cancel_requested is True
    assert orch._drain_mode is True
    assert "manual_test" in orch._cancel_reason

    # T1 + T2 dispatched on tick 1 (max_parallel_subs=2). T3 + T4 NEVER
    # dispatched because cancel fired on tick 2 before they could be
    # selected.
    dispatched_idents = [t.identifier for t in dispatched_order]
    assert dispatched_idents == ["SAL-1", "SAL-2"], (
        f"expected only T1 + T2 dispatched, got {dispatched_idents}"
    )
    # T1 + T2 completed normally (in-flight allowed to drain).
    assert t1.status == TicketStatus.MERGED_GREEN
    assert t2.status == TicketStatus.MERGED_GREEN
    # T3 + T4 stayed pending (never dispatched).
    assert t3.status == TicketStatus.PENDING, (
        f"T3 should never have left PENDING; got {t3.status}"
    )
    assert t4.status == TicketStatus.PENDING, (
        f"T4 should never have left PENDING; got {t4.status}"
    )

    # State events fired.
    event_kinds = [e["kind"] for e in orch.state.events]
    assert "cancel_requested" in event_kinds
    assert "wave_dispatch_canceled" in event_kinds
    cancel_ev = next(e for e in orch.state.events if e["kind"] == "cancel_requested")
    assert cancel_ev["status"] == "failed"
    assert cancel_ev["cancel_flag"] is True

    # Cancel log line emitted.
    cancel_logs = [
        r.getMessage() for r in caplog.records
        if "[cancel]" in r.getMessage()
    ]
    assert any("external cancel signal observed" in m for m in cancel_logs), (
        f"expected '[cancel] external cancel signal observed' log; got: {cancel_logs}"
    )
    assert any("drained (no in-flight)" in m for m in cancel_logs), (
        f"expected '[cancel] wave N drained' log; got: {cancel_logs}"
    )


async def test_check_cancel_signal_idempotent_and_handles_missing_record():
    """`_check_cancel_signal` returns False if the kickoff record isn't
    found (mesh returned None) and True only on the first cancel event.
    Subsequent calls after `_cancel_requested` is set short-circuit to
    False so the dispatch loop doesn't double-record events.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    # No record → False, no state mutation.
    assert await orch._check_cancel_signal() is False
    assert orch._cancel_requested is False

    # Add a get_task that returns a cancel record. First call: True.
    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "failed",
            "result": {"cancel": True, "reason": "test_cancel"},
        }
    mesh.get_task = _get_task

    assert await orch._check_cancel_signal() is True
    assert orch._cancel_requested is True
    assert orch._drain_mode is True
    assert orch._cancel_reason == "test_cancel"

    # Second call: idempotent, returns False without re-recording.
    initial_event_count = len(
        [e for e in orch.state.events if e["kind"] == "cancel_requested"]
    )
    assert await orch._check_cancel_signal() is False
    final_event_count = len(
        [e for e in orch.state.events if e["kind"] == "cancel_requested"]
    )
    assert initial_event_count == final_event_count == 1


async def test_check_cancel_signal_recognizes_canceled_status():
    """Forward-compat: `status == "canceled"` fires the cancel even
    without the result.cancel flag. soul-svc v2.0.0 only allows
    completed|failed today, but the orchestrator pre-honours a future
    `canceled` lifecycle state.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "canceled",
            "result": {},
        }
    mesh.get_task = _get_task

    assert await orch._check_cancel_signal() is True
    assert orch._cancel_requested is True
    assert "canceled" in orch._cancel_reason


async def test_complete_kickoff_canceled_posts_failed_with_cancel_flag(monkeypatch):
    """`_complete_kickoff_canceled` writes mesh.complete with
    status="failed" and result.cancel=True so the kickoff record
    clearly reflects an operator-driven stop.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    orch._cancel_requested = True
    orch._cancel_reason = "test_reason"
    orch.state.cumulative_spend_usd = 1.23

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(orch, "_snapshot_graph_into_state", lambda: None)

    await orch._complete_kickoff_canceled()

    assert len(mesh.completions) == 1
    rec = mesh.completions[0]
    assert rec["task_id"] == orch.task_id
    assert rec["status"] == "failed"
    assert rec["result"]["cancel"] is True
    assert rec["result"]["cancel_reason"] == "test_reason"
    assert "external_cancel" in rec["result"]["error"]
    assert "final_state_snapshot" in rec["result"]

