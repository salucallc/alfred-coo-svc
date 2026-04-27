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


# ── 2026-04-27 builder propose_pr APE/V citation reliability fix ────────────
#
# 75% of hawkman REQUEST_CHANGES in the 2026-04-26 v7af window were
# "missing APE/V citation". Root cause: builders paraphrased the
# acceptance text instead of byte-verbatim-copying from the Linear
# ticket body's `## APE/V Acceptance (machine-checkable)` section. Fix:
# the orchestrator pre-renders the canonical APE/V block into the
# dispatched task body so the builder can copy-paste with no ambiguity.
# Evidence: builder_reliability_2026-04-27.md.


def test_child_task_body_embeds_canonical_apev_when_linear_returns_text(
    monkeypatch,
):
    """When Linear returns a canonical `## APE/V Acceptance
    (machine-checkable)` section, the dispatched child body MUST embed
    that text under the canonical heading so the builder copies it
    byte-verbatim into the PR body. This closes the prompt gap that
    made paraphrase the dominant 75% reject reason.
    """
    canonical = (
        "- given X happens, when Y is invoked, then Z is true and green\n"
        "- (action_class, risk_tier) tuples enumerated; no enum drift\n"
        "- pytest tests/test_invariants.py green"
    )

    def fake_fetcher(code):
        # Verify the orchestrator passes the ticket code through.
        assert code == "SAL-2641"
        return canonical

    monkeypatch.setattr(
        "alfred_coo.tools._fetch_linear_acceptance_criteria",
        fake_fetcher,
    )

    orch = _mk_orchestrator()
    ticket = _t("u-2641", "SAL-2641", "OPS-08", 4, "ops")
    body = orch._child_task_body(ticket)

    # Canonical heading present — what hawkman validates against.
    assert "## APE/V Acceptance (machine-checkable)" in body, (
        "child body must surface the canonical hawkman heading"
    )
    # Verbatim text from Linear is embedded byte-for-byte.
    assert canonical in body, (
        "Linear acceptance text must appear byte-verbatim in dispatched "
        "body (no paraphrasing, no reformatting)"
    )
    # The instruction to copy verbatim into the PR body is also present.
    assert "byte-for-byte" in body or "byte-verbatim" in body.lower(), (
        "child body must instruct the builder to copy byte-verbatim"
    )


def test_child_task_body_falls_back_to_placeholder_when_linear_unavailable(
    monkeypatch,
):
    """A Linear hiccup (no key, transport error, missing section) MUST
    NOT block dispatch. The body falls back to the legacy placeholder
    checklist + a note pointing the builder at the plan doc.
    """
    def fake_fetcher(code):
        return None

    monkeypatch.setattr(
        "alfred_coo.tools._fetch_linear_acceptance_criteria",
        fake_fetcher,
    )

    orch = _mk_orchestrator()
    ticket = _t("u-na", "SAL-9999", "OPS-NA", 1, "ops")
    body = orch._child_task_body(ticket)

    # Legacy fallback heading still present so the builder gets *some*
    # APE/V scaffold.
    assert "## Acceptance (APE/V)" in body or (
        "## APE/V Acceptance (machine-checkable)" in body
    ), "fallback path must still emit some APE/V scaffold"
    # The fallback must direct the builder to fetch via http_get on
    # the plan doc (Step 1(b) path).
    assert "plan doc" in body.lower(), (
        "fallback must point the builder at the plan-doc fetch path"
    )


def test_child_task_body_falls_back_when_fetcher_raises(monkeypatch):
    """Defensive: any exception from the fetcher (transport hiccup,
    JSON decode error, etc.) must be swallowed and dispatch must
    proceed with the legacy placeholder.
    """
    def boom(code):
        raise RuntimeError("simulated transport error")

    monkeypatch.setattr(
        "alfred_coo.tools._fetch_linear_acceptance_criteria",
        boom,
    )

    orch = _mk_orchestrator()
    ticket = _t("u-boom", "SAL-2222", "OPS-BOOM", 1, "ops")
    # Must not raise.
    body = orch._child_task_body(ticket)
    assert isinstance(body, str) and body, "body must be a non-empty string"
    # Falls through to placeholder.
    assert "## Acceptance (APE/V)" in body, (
        "exception path must surface the fallback placeholder, not crash"
    )


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

    # One wave-0 ticket, dispatched, awaiting completion. SAL-2870:
    # opt out of retry-budget so this regression test can pin the
    # terminal-FAILED transition directly. Default budget would route
    # the ticket through BACKED_OFF (correct new behaviour, but a
    # different surface).
    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-1"
    t.retry_budget = 0  # SAL-2870: pin legacy terminal-FAILED behavior
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


# ── SAL-2978 · silent-complete envelope rejection ─────────────────────────


@pytest.mark.parametrize("envelope,expected,case", [
    # Truncated tool-loop envelope (dominant Mode A shape from v7aa).
    (
        {"content": "[tool-use loop exceeded max iterations]",
         "truncated": True, "tool_calls": [], "iterations": 12},
        True, "truncated_envelope",
    ),
    # Empty summary, no follow-up, no artifacts.
    ({"summary": "", "follow_up_tasks": [], "tool_calls": []}, True, "empty_summary"),
    # Whitespace-only summary.
    ({"summary": "   \n  \t ", "tool_calls": []}, True, "whitespace_summary"),
    # Missing / non-dict results — worst silent case.
    (None, True, "none_result"),
    ("oops", True, "string_result"),
    ([], True, "list_result"),
    # Valid envelope: real summary content.
    (
        {"summary": "Opened PR https://github.com/foo/bar/pull/1.",
         "follow_up_tasks": [], "tool_calls": []},
        False, "valid_envelope",
    ),
    # Empty summary but populated follow-up list: still actionable.
    (
        {"summary": "",
         "follow_up_tasks": ["queue SAL-9999 to retry with bigger context"]},
        False, "empty_summary_with_followup",
    ),
    # Empty summary but populated artifacts: still actionable.
    (
        {"summary": "", "artifacts": [{"path": "x.md", "content": "..."}]},
        False, "empty_summary_with_artifacts",
    ),
])
def test_envelope_is_silent_complete_classification(envelope, expected, case):
    """SAL-2978: shape classifier covers truncated, empty-summary, non-dict
    silent shapes; rejects valid envelopes + empty-summary-with-followup
    + empty-summary-with-artifacts as actionable.
    """
    from alfred_coo.autonomous_build.orchestrator import (
        AutonomousBuildOrchestrator,
    )
    assert (
        AutonomousBuildOrchestrator._envelope_is_silent_complete(envelope)
        is expected
    ), f"case={case}"


async def test_envelope_validator_rejects_silent_complete(monkeypatch, caplog):
    """SAL-2978 acceptance criterion: a silent-complete envelope routes
    the ticket to FAILED with a clear error log + `silent_complete`
    failure reason on the recorded event. This is the defense-in-depth
    backstop for the iteration-cap fix in main.py.
    """
    import logging as _logging

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

    t = _t("u1", "SAL-2588", "TIR-06", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-silent-1"
    t.retry_budget = 0  # pin terminal-FAILED behaviour for the assertion
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Simulate the truncated tool-loop envelope from `_tool_loop` (the
    # MAX_TOOL_ITERATIONS partial). No summary, truncated=True, no PR.
    mesh.completed_tasks.append({
        "id": "child-silent-1",
        "title": (
            "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2588 TIR-06 "
            "— fix: round 1 (...)"
        ),
        "status": "completed",
        "result": {
            "content": "[tool-use loop exceeded max iterations; partial]",
            "truncated": True,
            "tool_calls": [],
            "iterations": 12,
        },
    })

    with caplog.at_level(
        _logging.ERROR,
        logger="alfred_coo.autonomous_build.orchestrator",
    ):
        await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED for silent-complete envelope, got {t.status}"
    )
    assert ("SAL-2588", "Backlog") in linear_calls, (
        f"expected Linear rollback to Backlog, got: {linear_calls}"
    )
    # The ticket_failed event must carry the explicit `silent_complete`
    # reason so wave-gate math + ops triage can disambiguate from a
    # generic no-PR fail.
    silent_events = [
        ev for ev in orch.state.events
        if ev.get("kind") == "ticket_failed"
        and ev.get("reason") == "silent_complete"
    ]
    assert silent_events, (
        f"expected a ticket_failed event with reason=silent_complete; "
        f"got events: "
        f"{[ev for ev in orch.state.events if ev.get('kind') == 'ticket_failed']}"
    )
    # And a clear ERROR log line was emitted so ops can grep on SAL-2978.
    sal_logs = [
        r for r in caplog.records
        if r.levelno == _logging.ERROR and "SAL-2978" in r.message
    ]
    assert sal_logs, (
        f"expected a SAL-2978 ERROR log line; got: "
        f"{[r.message for r in caplog.records]}"
    )


async def test_envelope_validator_accepts_valid_envelope(monkeypatch):
    """SAL-2978 acceptance criterion (regression): a valid envelope with
    a real PR URL and summary follows the happy path → PR_OPEN → REVIEWING,
    NOT FAILED. Don't break the happy path with the new validator.
    """
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

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-happy-1"
    _seed_graph(orch, [t])

    # Stub out review dispatch so we don't need to mock the full review path.
    async def _fake_review(ticket):
        ticket.review_task_id = "review-1"
    monkeypatch.setattr(orch, "_dispatch_review", _fake_review)

    async def _fake_update(ticket, state_name):
        pass
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    mesh.completed_tasks.append({
        "id": "child-happy-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-1 TIR-01 ...",
        "status": "completed",
        "result": {
            "summary": (
                "Opened PR https://github.com/salucallc/alfred-coo-svc/pull/123 "
                "with the new TIR-01 module."
            ),
            "follow_up_tasks": [],
            "tool_calls": [],
        },
    })

    await orch._poll_children()

    # Valid envelope → REVIEWING (PR_OPEN → REVIEWING transition fires
    # immediately because `_dispatch_review` is stubbed).
    assert t.status == TicketStatus.REVIEWING, (
        f"expected REVIEWING for valid envelope, got {t.status}"
    )
    assert t.pr_url == "https://github.com/salucallc/alfred-coo-svc/pull/123"
    # No silent_complete event recorded.
    silent_events = [
        ev for ev in orch.state.events
        if ev.get("kind") == "ticket_failed"
        and ev.get("reason") == "silent_complete"
    ]
    assert not silent_events, (
        f"happy path falsely flagged silent_complete: {silent_events}"
    )


# ── AB-17-x · phantom-child reconciliation (post-v7k 2026-04-25) ────────────
#
# Reproduces the silent-stuck scenario observed twice on 2026-04-25:
# v7i (kickoff 06:11 UTC) and v7k (kickoff 07:14 UTC) both wedged on
# SAL-2672 SS-11 with `[watchdog] in_flight=1 ready=0` for hours, even
# though the mesh-state side showed zero claimed tasks for the run.
# Root scenario: a ticket is DISPATCHED → child completes → some path
# leaves the orchestrator's internal state pointing at a child_task_id
# that is no longer in mesh ``claimed`` AND not visible in the
# ``completed`` window the orchestrator polls. Without reconciliation,
# the ticket stays in_flight forever.


async def test_poll_children_force_fails_phantom_after_threshold(monkeypatch):
    """AB-17-x: a ticket whose ``child_task_id`` is missing from mesh
    claimed/completed/failed for >STUCK_CHILD_FORCE_FAIL_SEC must be
    force-failed so the dispatch loop unsticks. This is the post-v7k
    reconciliation patch (2026-04-25)."""
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

    mesh = _FakeMesh()  # no completed, no failed, no claimed records
    orch = _mk_orchestrator(
        kickoff_desc={
            "linear_project_id": "p",
            "budget": {"max_usd": 30},
            "wave_order": [0],
            "on_all_green": [],
        },
        mesh=mesh,
    )

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-phantom-1"
    t.retry_budget = 0  # SAL-2870: pin legacy terminal-FAILED behavior
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Seed the per-ticket transition timestamp to "long ago" so the stuck
    # threshold trips on the first poll. _snapshot_graph_into_state would
    # populate this in production; we set it directly to keep the test
    # focused on the reconcile path, not the snapshot machinery.
    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 10
    )

    updated = await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED for phantom child, got {t.status}; the orchestrator "
        f"must reconcile in_flight against mesh state to break silent-stuck"
    )
    assert ("SAL-1", "Backlog") in linear_calls, (
        f"phantom-child fail must roll Linear back to Backlog: {linear_calls}"
    )
    assert any(
        evt.get("kind") == "ticket_failed"
        and "phantom_child" in str(evt.get("note", ""))
        for evt in (orch.state.events or [])
    ), (
        f"expected a 'phantom_child' ticket_failed event in state, got: "
        f"{orch.state.events}"
    )
    assert t in updated


async def test_poll_children_does_not_force_fail_below_threshold(monkeypatch):
    """AB-17-x: a phantom ticket that's only been DISPATCHED briefly
    (sub-threshold) must NOT be force-failed — the brief window between
    PATCH /complete and the next ?status=completed read can legitimately
    show "missing from claimed AND completed" for sub-second durations."""
    mesh = _FakeMesh()  # no records at all
    orch = _mk_orchestrator(
        kickoff_desc={
            "linear_project_id": "p",
            "budget": {"max_usd": 30},
            "wave_order": [0],
            "on_all_green": [],
        },
        mesh=mesh,
    )

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-fresh-1"
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Mark the ticket as "just transitioned" — well under the stuck cap.
    import time as _time
    orch._ticket_transition_ts[t.id] = _time.time()

    await orch._poll_children()

    # No state change yet — DISPATCHED bumps to IN_PROGRESS (the existing
    # rec-is-None heuristic), but NOT to FAILED.
    assert t.status == TicketStatus.IN_PROGRESS, (
        f"sub-threshold phantom must NOT be force-failed; got {t.status}"
    )
    assert linear_calls == [], (
        f"no Linear updates expected for sub-threshold poll: {linear_calls}"
    )


async def test_poll_children_handles_mesh_failed_status(monkeypatch):
    """AB-17-x: a child task in mesh ``status=failed`` must surface in
    ``_poll_children`` and mark its ticket FAILED (with reason from the
    mesh record). Previously the orchestrator only fetched
    ``status=completed`` so failed children were invisible — they'd stay
    in_flight until the phantom-stuck timer caught them, costing 30 min
    of wedged dispatch loop per occurrence."""
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

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-failed-1"
    t.retry_budget = 0  # SAL-2870: pin legacy terminal-FAILED behavior
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    mesh.completed_tasks.append({
        "id": "child-failed-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-1 TIR-01 ...",
        "status": "failed",
        "result": {"error": "dispatch failure: TimeoutError: model timeout"},
    })

    await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED from mesh status=failed; got {t.status}"
    )
    # When the mesh record itself is failed, we route Linear → Canceled
    # (existing behaviour for the task_status=='failed' branch).
    assert ("SAL-1", "Canceled") in linear_calls, (
        f"expected Linear -> Canceled for mesh-failed child: {linear_calls}"
    )


async def test_poll_children_keeps_claimed_child_in_progress(monkeypatch):
    """AB-17-x: a child currently in mesh ``status=claimed`` is genuinely
    in flight — bump DISPATCHED→IN_PROGRESS but do NOT force-fail even
    if the per-ticket transition timestamp is ancient."""
    import time as _time
    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

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

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-claimed-1"
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Child IS still claimed (running on a long task). Even with an
    # ancient transition_ts, we must not force-fail it.
    mesh.completed_tasks.append({
        "id": "child-claimed-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] long-running",
        "status": "claimed",
        "result": None,
    })
    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 600
    )

    await orch._poll_children()

    assert t.status == TicketStatus.IN_PROGRESS, (
        f"claimed child must NOT be force-failed; got {t.status}"
    )
    assert linear_calls == [], (
        f"no Linear updates expected for in-progress claimed child: "
        f"{linear_calls}"
    )


# ── AB-17-y · orphan-active reconciliation (post-v7l, SAL-2842) ─────────────
#
# Reproduces the silent-stuck scenario observed on v7l 2026-04-25:
# SAL-2603 (UUID 28b30b6e...) hydrated from a prior daemon's persisted
# state as ``in_progress`` but with NO entry in
# ``state.dispatched_child_tasks``. The AB-17-x reconciler is gated on
# ``t.child_task_id`` being truthy — orphan-active tickets bypass it
# entirely. Watchdog reported ``in_flight=1 ready=0`` for 70+ min with
# no possible escape path.
#
# These tests pin the orphan-active reconciler so a future refactor that
# tightens the active-states list or moves the reconcile pre-pass can't
# silently regress the v7l fix.


async def test_poll_children_force_fails_orphan_active_after_threshold(
    monkeypatch,
):
    """AB-17-y: a ticket in ACTIVE_TICKET_STATES with ``child_task_id is
    None`` past STUCK_CHILD_FORCE_FAIL_SEC must be force-failed. Live
    bug: SAL-2603 stuck IN_PROGRESS for 70+ min on v7l with no recovery
    path. AB-17-x's reconcile loop skips this case because its filter
    requires ``t.child_task_id`` to be truthy."""
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

    mesh = _FakeMesh()  # no records anywhere
    orch = _mk_orchestrator(
        kickoff_desc={
            "linear_project_id": "p",
            "budget": {"max_usd": 30},
            "wave_order": [0],
            "on_all_green": [],
        },
        mesh=mesh,
    )

    t = _t("u-orphan", "SAL-2603", "ALT-06", 0, "aletheia",
           size="M", estimate=5)
    t.status = TicketStatus.IN_PROGRESS
    # The defining condition: active state, no child_task_id.
    assert t.child_task_id is None, "fixture invariant"
    # SAL-2870 phantom-child carve-out: pin retry_budget=0 so this test
    # asserts the AB-17-y FORCE-FAIL itself (terminal FAILED). Carve-out
    # behavior (FAILED -> PENDING with retry-count unchanged) has its
    # own dedicated tests below.
    t.retry_budget = 0
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Stuck way past the threshold (70 min, mirrors the live v7l case).
    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 2400
    )

    updated = await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED for orphan-active ticket, got {t.status}; the "
        f"orchestrator must reconcile active-state tickets with no "
        f"child_task_id or the watchdog will spin in_flight=1 forever"
    )
    assert ("SAL-2603", "Backlog") in linear_calls, (
        f"orphan-active force-fail must roll Linear -> Backlog: "
        f"{linear_calls}"
    )
    assert any(
        evt.get("kind") == "ticket_failed"
        and "no_child_task_id" in str(evt.get("note", ""))
        for evt in (orch.state.events or [])
    ), (
        f"expected a 'no_child_task_id' ticket_failed event in state, "
        f"got: {orch.state.events}"
    )
    assert t in updated, (
        f"orphan force-fail must surface in returned updated list (so "
        f"watchdog progress timestamp bumps); got: {updated}"
    )


async def test_poll_children_does_not_force_fail_orphan_below_threshold(
    monkeypatch,
):
    """AB-17-y: an orphan-active ticket whose transition_ts is RECENT
    must NOT be force-failed. The dispatch loop may legitimately
    transition a ticket to DISPATCHED before the next snapshot stamps
    ``child_task_id`` into state; the no-child window is bounded by
    one tick. Force-failing on the first tick post-restore would race
    the dispatch loop and create false-failures."""
    import time as _time

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

    t = _t("u-fresh", "SAL-1", "TIR-01", 0, "tiresias", size="S",
           estimate=1)
    t.status = TicketStatus.DISPATCHED
    assert t.child_task_id is None, "fixture invariant"
    _seed_graph(orch, [t])

    linear_calls = []
    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Just transitioned — well below threshold.
    orch._ticket_transition_ts[t.id] = _time.time()

    await orch._poll_children()

    assert t.status == TicketStatus.DISPATCHED, (
        f"sub-threshold orphan must NOT be force-failed; got {t.status}"
    )
    assert linear_calls == [], (
        f"no Linear updates for sub-threshold orphan: {linear_calls}"
    )


async def test_poll_children_orphan_reconcile_runs_when_no_in_flight(
    monkeypatch,
):
    """AB-17-y: the orphan reconcile MUST run even when the AB-17-x
    in-flight set is empty. Pre-fix, ``_poll_children`` returned early
    on ``if not in_flight: return []``, so an orphan ticket (which by
    definition has no child_task_id and is excluded from the in-flight
    filter) was never inspected. The orphan branch runs first and its
    output must be returned even on the empty-in-flight short-circuit.
    """
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

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

    # Only one ticket in the graph — orphan-active. No tickets with
    # ``child_task_id`` set, so the AB-17-x in_flight list is empty.
    t = _t("u-only-orphan", "SAL-2603", "ALT-06", 0, "aletheia",
           size="M", estimate=5)
    t.status = TicketStatus.IN_PROGRESS
    # SAL-2870 phantom-child carve-out: pin retry_budget=0 so this test
    # asserts the AB-17-y FORCE-FAIL pre-pass runs (terminal FAILED) even
    # when the in-flight short-circuit fires. Carve-out path (FAILED ->
    # PENDING) has its own dedicated tests below.
    t.retry_budget = 0
    _seed_graph(orch, [t])

    async def _fake_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 60
    )

    updated = await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"orphan reconcile must run BEFORE the in_flight short-circuit; "
        f"got {t.status}"
    )
    assert t in updated, (
        f"empty-in-flight short-circuit must still return orphan-failed "
        f"tickets; got: {updated}"
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
    """Hint count baseline. Bumped 22 -> 46 by PR #73 (hint-coverage 2026-04-25)
    which added 24 wave-1 entries: TIR-03..06, ALT-02/03/04/06, F04/F05/F12,
    OPS-04/05/16/17/23, SS-03/04/10/11/12, C-26/27/28."""
    assert len(_TARGET_HINTS) == 46


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
    # SAL-2870: collapse grace + retry so this AB-17-n test isolates
    # the pure deadlock-coerce semantics without waiting for the new
    # 15-min grace window or routing through BACKED_OFF.
    orch.deadlock_grace_sec = 0

    # T1 already FAILED (wave-0 child died without PR). T2 and T3 have
    # blocks_in=[T1.id] so they will flip PENDING -> BLOCKED on the very
    # first `_select_ready` tick and remain stuck forever absent the
    # detector.
    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t2 = _t("u2", "SAL-2", "TIR-02", 0, "tiresias", blocks_in=["u1"])
    t3 = _t("u3", "SAL-3", "TIR-03", 0, "tiresias", blocks_in=["u1"])
    t1.status = TicketStatus.FAILED
    # SAL-2870: T1 is the failing upstream the test fixture pretends has
    # already exhausted retries. Pin retry_budget=0 on T2/T3 so the post-
    # pass doesn't bounce them through BACKED_OFF instead of FAILED.
    t1.retry_budget = 0
    t2.retry_budget = 0
    t3.retry_budget = 0
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


# ── SAL-2890: defend _check_cancel_signal against self-inflicted cancels ───
#
# v7p resume run at 21:41 UTC 2026-04-25: the daemon's main task-claim loop
# spuriously re-claimed its OWN already-running orchestrator parent task. The
# duplicate-kickoff guard in main.py rejected the second claim by setting
# mesh status=failed with reason
#   "duplicate_kickoff: existing orchestrator task=<own_id> running for project=<id>"
# The still-running orchestrator's `_check_cancel_signal` polled the parent
# task on its next tick, observed status=failed, and treated it as an
# external cancel. Self-inflicted: PR #91 was actively recovering. Layer-A
# fix at the cancel-signal poll site filters self-inflicted reasons.


async def test_check_cancel_signal_rejects_self_inflicted_duplicate_kickoff(caplog):
    """SAL-2890: when the failed-kickoff reason is a duplicate-kickoff guard
    rejection naming the orchestrator's OWN task id, the cancel signal must
    be ignored — that's the daemon's own main loop racing itself, not an
    external operator stop. A WARNING is logged so the race stays visible.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    own_id = orch.task_id  # "kick-abc" via _mk_orchestrator
    project_id = "8c1d8f69-aaaa-bbbb-cccc-deadbeefcafe"

    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "failed",
            "result": {
                "reason": (
                    f"duplicate_kickoff: existing orchestrator task={own_id} "
                    f"running for project={project_id}"
                ),
            },
        }
    mesh.get_task = _get_task

    import logging as _logging
    with caplog.at_level(
        _logging.WARNING,
        logger="alfred_coo.autonomous_build.orchestrator",
    ):
        observed = await orch._check_cancel_signal()

    assert observed is False, "self-inflicted duplicate-kickoff must NOT fire cancel"
    assert orch._cancel_requested is False
    assert orch._drain_mode is False
    assert orch._cancel_reason == ""

    # No cancel_requested event recorded.
    cancel_events = [e for e in orch.state.events if e["kind"] == "cancel_requested"]
    assert cancel_events == []

    # SAL-2890 WARNING emitted — leaves a breadcrumb for the race.
    sal_logs = [
        r.getMessage() for r in caplog.records
        if r.levelname == "WARNING" and "SAL-2890" in r.getMessage()
    ]
    assert len(sal_logs) == 1, (
        f"expected one SAL-2890 self-inflicted-cancel WARNING; got: {sal_logs}"
    )
    assert "self-inflicted" in sal_logs[0].lower() or "ignoring" in sal_logs[0].lower()


async def test_check_cancel_signal_honors_duplicate_kickoff_for_different_task_id():
    """The self-inflicted filter must only fire when the duplicate-kickoff
    reason names the orchestrator's OWN task id. A duplicate-kickoff message
    referencing a DIFFERENT task id (e.g. genuinely orphaned upstream task)
    should still be honored as a cancel — guards against the filter being
    too permissive.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    other_id = "deadbeef-1111-2222-3333-444444444444"
    project_id = "8c1d8f69-aaaa-bbbb-cccc-deadbeefcafe"
    assert other_id != orch.task_id

    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "failed",
            "result": {
                "reason": (
                    f"duplicate_kickoff: existing orchestrator task={other_id} "
                    f"running for project={project_id}"
                ),
            },
        }
    mesh.get_task = _get_task

    observed = await orch._check_cancel_signal()
    assert observed is True
    assert orch._cancel_requested is True
    assert orch._drain_mode is True
    assert "duplicate_kickoff" in orch._cancel_reason


async def test_check_cancel_signal_honors_external_cancel():
    """Regression: an explicit external-cancel reason must still fire the
    cancel — operators must be able to stop a runaway wave with
    `mesh.complete --status failed --reason external_cancel`.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "failed",
            "result": {"reason": "external_cancel: operator stop"},
        }
    mesh.get_task = _get_task

    observed = await orch._check_cancel_signal()
    assert observed is True
    assert orch._cancel_requested is True
    assert orch._drain_mode is True
    assert "external_cancel" in orch._cancel_reason


async def test_check_cancel_signal_honors_naked_failed_status():
    """Regression: AB-17-q's documented behaviour — `status="failed"` with
    no `cancel` flag and no `reason` is still treated as a cancel. The
    SAL-2890 self-inflicted filter must not regress this path.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    async def _get_task(task_id):
        return {
            "id": task_id,
            "status": "failed",
            "result": {},
        }
    mesh.get_task = _get_task

    observed = await orch._check_cancel_signal()
    assert observed is True
    assert orch._cancel_requested is True
    assert orch._drain_mode is True
    # Synthesised reason from the no-explicit-reason branch.
    assert "external_cancel" in orch._cancel_reason
    assert "failed" in orch._cancel_reason


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


# ── SAL-2870: dependency-aware retry + deadlock grace + topo dispatch ──────
#
# v7o crashed at 18:09:19 UTC 2026-04-25 with `wave 1 deadlock: 17 tickets
# non-terminal with no in-flight or ready; coercing to FAILED`. The 17
# downstream tickets were BLOCKED on FAILED upstreams (SS-10 et al.).
# AB-17-n's same-tick coerce-to-FAILED cascaded the entire wave-1 tail to
# FAILED before any retry could land. SAL-2870 introduces:
#   1. Per-ticket retry budget — FAILED -> BACKED_OFF -> PENDING re-dispatch
#   2. BACKED_OFF timer -> PENDING flip-back after retry_backoff_sec
#   3. Re-evaluate downstream readiness on every tick (not just on
#      MERGED_GREEN transition)
#   4. Deadlock detector grace period (15 min default) before coerce
#   5. Topological dispatch order within wave (deps first)


async def test_sal_2870_retry_failed_to_backed_off(monkeypatch):
    """Component #1: a ticket FAILED with retry budget remaining is
    routed through BACKED_OFF instead of terminal FAILED, with
    retry_count incremented and child_task_id cleared so the next
    dispatch creates a fresh sub.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    # Tunables: budget=2, backoff=300s (default).
    assert orch.retry_budget == 2
    assert orch.retry_backoff_sec == 300

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-1"
    _seed_graph(orch, [t])

    async def _fake_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Child completes without a PR -> orchestrator wants to mark FAILED.
    mesh.completed_tasks.append({
        "id": "child-1",
        "status": "completed",
        "result": {"summary": "no PR opened"},
    })

    await orch._poll_children()

    # Retry sweep should bounce the FAILED to BACKED_OFF.
    assert t.status == TicketStatus.BACKED_OFF, (
        f"expected BACKED_OFF after retry sweep, got {t.status}"
    )
    assert t.retry_count == 1
    assert t.child_task_id is None  # cleared for fresh dispatch
    assert t.backed_off_at is not None and t.backed_off_at > 0

    # state.events should record both the failure and the back-off.
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_failed" in kinds
    assert "ticket_backed_off" in kinds
    bo = next(e for e in orch.state.events if e["kind"] == "ticket_backed_off")
    assert bo["identifier"] == "SAL-1"
    assert bo["retry_count"] == 1
    assert bo["retry_budget"] == 2


async def test_sal_2870_retry_exhausted_lands_terminal_failed(monkeypatch):
    """Component #1: when retry_count == retry_budget, FAILED is
    terminal — no further BACKED_OFF bounce.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-2"
    t.retry_budget = 2
    t.retry_count = 2  # already exhausted
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    mesh.completed_tasks.append({
        "id": "child-2",
        "status": "completed",
        "result": {"summary": "no PR"},
    })
    await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"expected terminal FAILED on exhausted budget, got {t.status}"
    )
    assert t.retry_count == 2
    bo = [e for e in orch.state.events if e["kind"] == "ticket_backed_off"]
    assert not bo, "no back-off when budget exhausted"


async def test_sal_2870_retry_budget_zero_disables_retry(monkeypatch):
    """retry_budget=0 (legacy semantics) keeps FAILED terminal."""
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-3"
    t.retry_budget = 0
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    mesh.completed_tasks.append({
        "id": "child-3",
        "status": "completed",
        "result": {"summary": "no PR"},
    })
    await orch._poll_children()

    assert t.status == TicketStatus.FAILED


async def test_sal_2870_backed_off_wakes_after_window():
    """Component #2: a ticket BACKED_OFF at t=0 is still BACKED_OFF at
    t=4min, but at t=5min01s it wakes back to PENDING and is re-dispatched
    next tick.
    """
    import time
    orch = _mk_orchestrator()
    orch.retry_backoff_sec = 5 * 60  # 5 min default

    t = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t.status = TicketStatus.BACKED_OFF
    t.retry_count = 1
    t.retry_budget = 2
    t.backed_off_at = time.time() - (4 * 60)  # 4 min ago
    _seed_graph(orch, [t])

    woken = orch._wake_backed_off_tickets()
    assert woken == [], "ticket below backoff window should not wake"
    assert t.status == TicketStatus.BACKED_OFF

    # Advance: now elapsed = 5min 1s.
    t.backed_off_at = time.time() - (5 * 60 + 1)
    woken = orch._wake_backed_off_tickets()
    assert woken == [t]
    assert t.status == TicketStatus.PENDING
    assert t.backed_off_at is None
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_woke_from_backoff" in kinds


async def test_sal_2870_deadlock_grace_no_coerce_below_threshold(monkeypatch):
    """Component #4: 14 min of in_flight=0 + ready=0 must NOT coerce to
    FAILED (sub-grace). Only at 15 min does the detector fire.
    """
    import logging
    import time

    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    orch.deadlock_grace_sec = 15 * 60
    # Disable retry so the BLOCKED tickets don't get bounced through
    # BACKED_OFF (which would defeat the deadlock-only assertion).
    orch.retry_budget = 0

    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t2 = _t("u2", "SAL-2", "TIR-02", 0, "tiresias", blocks_in=["u1"])
    t1.status = TicketStatus.FAILED
    t1.retry_budget = 0
    t2.retry_budget = 0
    _seed_graph(orch, [t1, t2])

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

    # Hold time at "14 min after grace armed" — the detector should not
    # fire. We let the loop run a few ticks, then bail externally.
    real_time = time.time
    armed_at = real_time()
    def _fake_time():
        # Always 14 min past arm — never crosses the 15-min threshold.
        return armed_at + 14 * 60
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.time.time", _fake_time
    )

    ticks = {"n": 0}
    real_sleep = asyncio.sleep
    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] >= 5:
            # Force exit: flip both tickets so the loop's all-terminal
            # check trips. We're proving the detector did NOT coerce.
            t2.status = TicketStatus.FAILED  # external resolution
        await real_sleep(0)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    forced = [
        e for e in orch.state.events
        if e["kind"] == "ticket_forced_failed_deadlock"
    ]
    assert forced == [], (
        f"detector must NOT coerce within grace window; got {forced}"
    )


async def test_sal_2870_deadlock_grace_coerces_after_threshold(
    monkeypatch, caplog
):
    """Component #4: at 15:01 (>= grace) the detector fires and coerces
    BLOCKED tickets to FAILED.
    """
    import logging
    import time

    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    orch.deadlock_grace_sec = 15 * 60
    orch.retry_budget = 0

    t1 = _t("u1", "SAL-1", "TIR-01", 0, "tiresias")
    t2 = _t("u2", "SAL-2", "TIR-02", 0, "tiresias", blocks_in=["u1"])
    t1.status = TicketStatus.FAILED
    t1.retry_budget = 0
    t2.retry_budget = 0
    _seed_graph(orch, [t1, t2])

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

    # Pre-arm: set _no_progress_since to "16 minutes ago" so the very
    # first tick of _dispatch_wave's grace check sees stuck_for >= grace.
    # This bypasses the chicken-and-egg of mocking time.time across the
    # snapshot + watchdog + detector all in one tick.
    orch._no_progress_since = time.time() - (16 * 60)

    ticks = {"n": 0}
    real_sleep = asyncio.sleep
    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] > 10:
            raise RuntimeError("grace detector failed within 10 ticks")
        await real_sleep(0)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    with caplog.at_level(
        logging.ERROR, logger="alfred_coo.autonomous_build.orchestrator"
    ):
        await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    assert t2.status == TicketStatus.FAILED, (
        f"expected coerce-to-FAILED past grace, got {t2.status}"
    )
    forced = [
        e for e in orch.state.events
        if e["kind"] == "ticket_forced_failed_deadlock"
    ]
    assert len(forced) == 1
    assert forced[0]["identifier"] == "SAL-2"
    # stuck_for_sec field (new in SAL-2870) reports elapsed time.
    assert forced[0]["stuck_for_sec"] >= 15 * 60


def test_sal_2870_topo_sort_orders_deps_before_dependents():
    """Component #5: graph A->B->C, ready=[B, C, A] (out of order),
    after _topo_sort = [A, B, C].
    """
    orch = _mk_orchestrator()
    a = _t("ua", "SAL-A", "TIR-A", 0, "tiresias")
    b = _t("ub", "SAL-B", "TIR-B", 0, "tiresias", blocks_in=["ua"])
    c = _t("uc", "SAL-C", "TIR-C", 0, "tiresias", blocks_in=["ub"])
    _seed_graph(orch, [a, b, c])

    out = orch._topo_sort([b, c, a])
    assert [t.identifier for t in out] == ["SAL-A", "SAL-B", "SAL-C"], (
        f"expected [A, B, C], got {[t.identifier for t in out]}"
    )


def test_sal_2870_topo_sort_preserves_independent_ordering():
    """No edges among the ready set -> stable order falls back to
    (CP, identifier).
    """
    orch = _mk_orchestrator()
    a = _t("ua", "SAL-A", "TIR-A", 0, "tiresias")
    b = _t("ub", "SAL-B", "TIR-B", 0, "tiresias", is_critical_path=True)
    c = _t("uc", "SAL-C", "TIR-C", 0, "tiresias")
    _seed_graph(orch, [a, b, c])

    # CP first, then identifier: [B (CP), A, C].
    out = orch._topo_sort([c, a, b])
    assert [t.identifier for t in out] == ["SAL-B", "SAL-A", "SAL-C"]


def test_sal_2870_topo_sort_handles_cycles_gracefully():
    """A cycle (A->B->A) returns all tickets (in identifier order for the
    unreachable remainder) and emits a WARNING — never drops items.
    """
    import logging
    orch = _mk_orchestrator()
    a = _t("ua", "SAL-A", "TIR-A", 0, "tiresias", blocks_in=["ub"])
    b = _t("ub", "SAL-B", "TIR-B", 0, "tiresias", blocks_in=["ua"])
    _seed_graph(orch, [a, b])

    out = orch._topo_sort([a, b])
    assert {t.identifier for t in out} == {"SAL-A", "SAL-B"}


def test_sal_2870_select_ready_uses_topo_within_cp_tier():
    """Component #5 integration: _select_ready returns tickets in topo
    order within each critical-path tier.
    """
    orch = _mk_orchestrator()
    # Both CP, A blocks B. Even though B's identifier sorts ahead of A,
    # topo must put A first.
    a = _t("ua", "SAL-Z-A", "TIR-A", 0, "tiresias", is_critical_path=True)
    b = _t("ub", "SAL-A-B", "TIR-B", 0, "tiresias",
           is_critical_path=True, blocks_in=["ua"])
    a.blocks_out = ["ub"]
    _seed_graph(orch, [a, b])

    ready = orch._select_ready([a, b], in_flight=[])
    # A (no deps) must come before B even though "SAL-A-B" < "SAL-Z-A".
    # B is BLOCKED on A so doesn't appear in ready set yet.
    assert ready == [a]

    # Make A green; now B becomes ready.
    a.status = TicketStatus.MERGED_GREEN
    ready = orch._select_ready([a, b], in_flight=[])
    assert ready == [b]


async def test_sal_2870_cascading_unblock_on_retry(monkeypatch):
    """Cascading unblock: A FAILED -> BACKED_OFF; A retries -> MERGED_GREEN;
    B (depends on A) unblocks even though it was BLOCKED multiple ticks
    ago. The _refresh_blocked_status pre-pass in _poll_children handles
    this without needing an upstream transition event.
    """
    import time
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    a = _t("ua", "SAL-A", "TIR-A", 0, "tiresias")
    b = _t("ub", "SAL-B", "TIR-B", 0, "tiresias", blocks_in=["ua"])
    a.blocks_out = ["ub"]
    # Setup: A is in BACKED_OFF (just failed once), B is BLOCKED waiting
    # for A.
    a.status = TicketStatus.BACKED_OFF
    a.retry_count = 1
    a.retry_budget = 2
    a.backed_off_at = time.time() - 1000  # already past any sane backoff
    b.status = TicketStatus.BLOCKED
    _seed_graph(orch, [a, b])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    # Tick 1: A wakes from backoff (BACKED_OFF -> PENDING). B stays BLOCKED.
    await orch._poll_children()
    assert a.status == TicketStatus.PENDING
    assert b.status == TicketStatus.BLOCKED

    # External: A is dispatched and merges green.
    a.status = TicketStatus.MERGED_GREEN

    # Tick 2: orchestrator re-evaluates. B should unblock.
    await orch._poll_children()
    assert b.status == TicketStatus.PENDING, (
        f"B should unblock once A is MERGED_GREEN; got {b.status}"
    )
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_unblocked" in kinds


async def test_sal_2870_v7o_synthetic_cascade_recovers(monkeypatch):
    """Synthetic v7o-style cascade: TIR-02 fails on first attempt ->
    BACKED_OFF -> retries -> MERGED_GREEN -> TIR-03..06 unblock.
    Previously this whole cascade FAILED at the same-tick deadlock; now
    it succeeds.
    """
    import time
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    orch.retry_backoff_sec = 0  # zero-window for fast test
    orch.deadlock_grace_sec = 999999  # disable grace coerce

    # 5 tickets in a chain: TIR-02 -> TIR-03 -> ... -> TIR-06.
    chain = []
    prev_id = None
    for i in range(2, 7):
        kwargs = {}
        if prev_id:
            kwargs["blocks_in"] = [prev_id]
        t = _t(f"u{i}", f"SAL-{i}", f"TIR-0{i}", 0, "tiresias", **kwargs)
        chain.append(t)
        prev_id = t.id
    # Wire blocks_out for parity.
    for i in range(len(chain) - 1):
        chain[i].blocks_out = [chain[i + 1].id]

    tir_02 = chain[0]
    tir_03 = chain[1]
    tir_04 = chain[2]
    tir_05 = chain[3]
    tir_06 = chain[4]

    # TIR-02 starts DISPATCHED; downstream all BLOCKED.
    tir_02.status = TicketStatus.DISPATCHED
    tir_02.child_task_id = "child-tir02-1"
    for t in (tir_03, tir_04, tir_05, tir_06):
        t.status = TicketStatus.BLOCKED
    _seed_graph(orch, chain)

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    # Tick 1: TIR-02's first attempt fails (no PR).
    mesh.completed_tasks.append({
        "id": "child-tir02-1",
        "status": "completed",
        "result": {"summary": "no PR opened"},
    })
    await orch._poll_children()

    # Retry sweep should put TIR-02 in BACKED_OFF.
    assert tir_02.status == TicketStatus.BACKED_OFF
    assert tir_02.retry_count == 1
    # Downstream still BLOCKED.
    assert tir_03.status == TicketStatus.BLOCKED

    # Tick 2: backoff window is 0s -> TIR-02 wakes back to PENDING.
    mesh.completed_tasks.clear()
    await orch._poll_children()
    assert tir_02.status == TicketStatus.PENDING

    # Simulate retry: dispatch + merge green.
    tir_02.status = TicketStatus.MERGED_GREEN

    # Tick 3: downstream unblock cascade.
    await orch._poll_children()
    assert tir_03.status == TicketStatus.PENDING, (
        f"TIR-03 should unblock once TIR-02 is green; got {tir_03.status}"
    )
    # TIR-04..06 are still BLOCKED on TIR-03 etc — they don't all
    # unblock at once because the chain is sequential. But the
    # important assertion is that the recovery path is reachable.
    assert tir_04.status == TicketStatus.BLOCKED
    assert tir_05.status == TicketStatus.BLOCKED
    assert tir_06.status == TicketStatus.BLOCKED


async def test_sal_2870_payload_overrides_retry_tunables():
    """Component #1+2+4: kickoff payload's retry_budget,
    retry_backoff_sec, deadlock_grace_sec all flow through to the
    instance attributes. Bad values fall back to defaults with a
    warning (no crash).
    """
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p",
        "retry_budget": 5,
        "retry_backoff_sec": 600,
        "deadlock_grace_sec": 1800,
    })
    # _parse_payload runs lazily — call it directly.
    orch._parse_payload()
    assert orch.retry_budget == 5
    assert orch.retry_backoff_sec == 600
    assert orch.deadlock_grace_sec == 1800

    # Bad values: silent fallback to defaults, no exception.
    orch2 = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "p",
        "retry_budget": "not-a-number",
        "retry_backoff_sec": None,  # ignored
        "deadlock_grace_sec": [],  # rejected
    })
    orch2._parse_payload()
    assert orch2.retry_budget == 2  # default
    assert orch2.deadlock_grace_sec == 15 * 60  # default


async def test_sal_2870_state_round_trip_carries_retry_fields():
    """Snapshot/restore pins the retry_count + backed_off_at +
    no_progress_since fields so a daemon bounce doesn't reset retry
    state.
    """
    from alfred_coo.autonomous_build.state import OrchestratorState
    s = OrchestratorState(
        kickoff_task_id="k",
        retry_counts={"u1": 1, "u2": 2},
        backed_off_at={"u1": 1234.5},
        no_progress_since=999.0,
    )
    blob = s.to_json()
    s2 = OrchestratorState.from_json(blob)
    assert s2.retry_counts == {"u1": 1, "u2": 2}
    assert s2.backed_off_at == {"u1": 1234.5}
    assert s2.no_progress_since == 999.0


# ── SAL-2886 · escalate-path discriminator ─────────────────────────────────
#
# Reproduces the v7p wave-0 cascade (2026-04-25 evening): four already-merged
# tickets ran the documented escalate path (linear_create_issue ->
# grounding-gap), the orchestrator misclassified those completions as silent
# persona bugs (no PR URL == FAILED), then SAL-2870's retry-budget bounced
# every ticket through BACKED_OFF and burned retries on tickets that had
# nothing left to do. Fix: distinguish the escalate emit from the
# silent-bug emit BEFORE the FAILED fall-through, transition to a new
# ESCALATED terminal-non-failure state. Evidence:
# Z:/_evidence/v7p_child_envelopes_2026-04-25.json.


async def test_poll_children_grounding_gap_envelope_marks_escalated(
    monkeypatch,
):
    """SAL-2886 happy-path: a child completion whose tool_calls show the
    documented escalate-path emit (linear_create_issue returning a
    grounding-gap issue) must transition the ticket to ESCALATED — not
    FAILED — and must NOT roll Linear back to Backlog. Mirrors the v7p
    envelope shape captured in
    Z:/_evidence/v7p_child_envelopes_2026-04-25.json.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2886-x", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-grounding-gap-1"
    _seed_graph(orch, [t])

    linear_calls: list[tuple[str, str]] = []

    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # v7p envelope shape (verbatim from spec): single linear_create_issue
    # tool call whose result has a grounding-gap title + identifier.
    mesh.completed_tasks.append({
        "id": "child-grounding-gap-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2886-x ...",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "linear_create_issue",
                "result": {
                    "identifier": "SAL-2999",
                    "title": "grounding gap: SAL-X missing target",
                    "url": "https://linear.app/saluca/issue/SAL-2999",
                },
            }],
            "summary": "Escalated SAL-X due to conflicting target file entries",
        },
    })

    updated = await orch._poll_children()

    assert t.status == TicketStatus.ESCALATED, (
        f"expected ESCALATED for grounding-gap envelope, got {t.status}"
    )
    assert t in updated

    # State event recorded with the gap identifier.
    escalated_events = [
        e for e in orch.state.events if e["kind"] == "ticket_escalated"
    ]
    assert len(escalated_events) == 1, (
        f"expected one ticket_escalated event, got: {orch.state.events}"
    )
    assert escalated_events[0]["identifier"] == "SAL-2886-x"
    assert escalated_events[0]["grounding_gap"] == "SAL-2999"

    # No Linear -> Backlog transition: operator inspects the gap issue.
    assert ("SAL-2886-x", "Backlog") not in linear_calls, (
        f"escalate path must NOT roll Linear back to Backlog: {linear_calls}"
    )


async def test_poll_children_genuine_silent_persona_bug_still_marks_failed(
    monkeypatch,
):
    """SAL-2886 negative: an envelope with NO PR URL AND NO
    linear_create_issue grounding-gap call is the genuine silent-bug
    shape (the v7p fix must not weaken it). Such tickets stay FAILED
    with the legacy "child completed without PR URL" note.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2886-y", "TIR-02", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-silent-bug-1"
    t.retry_budget = 0  # SAL-2870: pin terminal-FAILED, no BACKED_OFF bounce
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    # Envelope: a tool call that is NOT linear_create_issue, no PR URL.
    mesh.completed_tasks.append({
        "id": "child-silent-bug-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2886-y ...",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "http_get",
                "result": "<html>...</html>",
            }],
            "summary": "I considered the task but did not open a PR",
        },
    })

    await orch._poll_children()

    assert t.status == TicketStatus.FAILED, (
        f"genuine silent-bug must still land terminal FAILED, got {t.status}"
    )
    failed_events = [
        e for e in orch.state.events if e["kind"] == "ticket_failed"
    ]
    assert any(
        e.get("note") == "child completed without PR URL"
        for e in failed_events
    ), (
        f"expected the legacy silent-bug note, got: {failed_events}"
    )
    # No spurious escalation event.
    assert not any(
        e["kind"] == "ticket_escalated" for e in orch.state.events
    ), "silent-bug envelope must NOT produce a ticket_escalated event"


async def test_retry_budget_sweep_skips_escalated(monkeypatch):
    """SAL-2886 + SAL-2870 interaction: a ticket whose escalate path
    fired (transitioned to ESCALATED) MUST NOT be picked up by the
    retry-budget sweep. ESCALATED is terminal-non-failure; retry_count
    must stay at 0 and _back_off_ticket must not be called. This is
    the regression test for the v7p cascade where 4 tickets burned
    retries on already-merged work.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    assert orch.retry_budget == 2  # default budget non-zero

    t = _t("u1", "SAL-2886-z", "TIR-03", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-grounding-gap-z"
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    # Spy on _back_off_ticket — must NOT be called for the ESCALATED ticket.
    back_off_calls: list[str] = []
    real_back_off = orch._back_off_ticket

    def _spy_back_off(ticket):
        back_off_calls.append(ticket.identifier)
        return real_back_off(ticket)

    monkeypatch.setattr(orch, "_back_off_ticket", _spy_back_off)

    mesh.completed_tasks.append({
        "id": "child-grounding-gap-z",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "linear_create_issue",
                "result": {
                    "identifier": "SAL-3000",
                    "title": "grounding gap: SAL-Z target merged in prior wave",
                },
            }],
        },
    })

    await orch._poll_children()

    assert t.status == TicketStatus.ESCALATED, (
        f"sanity: discriminator must classify as ESCALATED, got {t.status}"
    )
    assert back_off_calls == [], (
        f"retry-budget sweep must skip ESCALATED, but _back_off_ticket "
        f"was called for: {back_off_calls}"
    )
    assert t.retry_count == 0, (
        f"retry_count must not be incremented for ESCALATED, got "
        f"{t.retry_count}"
    )
    # And no ticket_backed_off event recorded.
    assert not any(
        e["kind"] == "ticket_backed_off" for e in orch.state.events
    ), "ESCALATED must not emit a ticket_backed_off event"


# ── SAL-2870 phantom-child carve-out (2026-04-26) ─────────────────────────
#
# Composition bug between AB-17-x/AB-17-y phantom force-fails and the
# SAL-2870 retry sweep: phantom cleanup is bookkeeping (no real build
# attempt happened), but the sweep was routing every phantom-FAILED
# through BACKED_OFF + retry_count++. Today's v7ab/v7ac live runs (post-
# daemon-restart, 2026-04-26) burned ~5+ min × 4 orphans = ~20 min idle
# per wave on the cooling window before fresh dispatch — and burned a
# retry slot on each.
#
# Fix: `last_failure_reason` tag set by phantom branches; sweep checks
# the tag, calls `_reset_phantom_failure` (FAILED -> PENDING, no
# retry_count bump, child_task_id cleared), skips BACKED_OFF entirely.
# Real failures (model crashes, silent_complete, no_pr_url, hawkman 3x
# REQUEST_CHANGES) still go through BACKED_OFF as before.


async def test_phantom_child_skips_backed_off_and_dispatches_immediately(
    monkeypatch,
):
    """SAL-2870 phantom carve-out: a ticket force-failed by AB-17-x's
    phantom-child reconciler (child_task_id missing from claimed/
    completed/failed) must short-circuit BACKED_OFF and land in PENDING
    so the next dispatch tick re-attaches a fresh child immediately.
    Retry counter must NOT be incremented — phantom cleanup is not a
    real build attempt and shouldn't burn the operator's retry budget.
    """
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

    mesh = _FakeMesh()  # no completed/failed/claimed records
    orch = _mk_orchestrator(mesh=mesh)
    # Default retry_budget=2 (phantom carve-out only kicks in when retry
    # is enabled; budget=0 keeps legacy terminal-FAILED for AB-17-x's
    # detection-focused tests).
    assert orch.retry_budget == 2

    t = _t("u1", "SAL-2870-p", "TIR-01", 0, "tiresias", size="S",
           estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-phantom-zzz"
    t.retry_count = 0
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    # Spy on _back_off_ticket — must NOT be called for phantom failure.
    back_off_calls: list[str] = []
    real_back_off = orch._back_off_ticket
    def _spy_back_off(ticket):
        back_off_calls.append(ticket.identifier)
        return real_back_off(ticket)
    monkeypatch.setattr(orch, "_back_off_ticket", _spy_back_off)

    # Trip the AB-17-x phantom-child threshold.
    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 60
    )

    updated = await orch._poll_children()

    # Carve-out outcome: PENDING (NOT BACKED_OFF, NOT terminal FAILED).
    assert t.status == TicketStatus.PENDING, (
        f"phantom-child cleanup must short-circuit to PENDING for "
        f"immediate re-dispatch, got {t.status}"
    )
    # Retry counter NOT incremented — bookkeeping isn't a real attempt.
    assert t.retry_count == 0, (
        f"phantom cleanup must not consume retry budget, got "
        f"retry_count={t.retry_count}"
    )
    # Cooling timer is unset (we're not BACKED_OFF).
    assert t.backed_off_at is None, (
        f"phantom cleanup must not set backed_off_at, got "
        f"{t.backed_off_at}"
    )
    # Stale child id cleared so next dispatch creates a fresh child
    # (otherwise phantom detection re-trips on the same id).
    assert t.child_task_id is None, (
        f"phantom cleanup must clear child_task_id; got "
        f"{t.child_task_id}"
    )
    # Tag cleared so a future REAL failure routes correctly through
    # BACKED_OFF without the phantom skip mis-firing.
    assert t.last_failure_reason is None, (
        f"phantom tag must be cleared post-reset; got "
        f"{t.last_failure_reason}"
    )
    # _back_off_ticket was NEVER called for this ticket.
    assert back_off_calls == [], (
        f"_back_off_ticket must not run for phantom cleanup; spy "
        f"saw: {back_off_calls}"
    )
    # No ticket_backed_off event recorded.
    assert not any(
        e["kind"] == "ticket_backed_off" for e in orch.state.events
    ), (
        f"phantom cleanup must not emit ticket_backed_off; events: "
        f"{orch.state.events}"
    )
    # The phantom-fail event AND the phantom-reset event ARE recorded
    # so operations can grep both signals.
    assert any(
        e["kind"] == "ticket_failed"
        and "phantom_child" in str(e.get("note", ""))
        for e in orch.state.events
    ), f"expected ticket_failed (phantom_child) event; got {orch.state.events}"
    reset_evts = [
        e for e in orch.state.events if e["kind"] == "ticket_phantom_reset"
    ]
    assert len(reset_evts) == 1, (
        f"expected exactly one ticket_phantom_reset event; got {reset_evts}"
    )
    assert reset_evts[0]["reason"] == "phantom_child"
    assert reset_evts[0]["retry_count"] == 0, (
        f"reset event must mirror unchanged retry_count=0; got "
        f"{reset_evts[0]}"
    )
    assert t in updated


async def test_orphan_active_skips_backed_off_and_dispatches_immediately(
    monkeypatch,
):
    """SAL-2870 phantom carve-out: AB-17-y orphan-active force-fail
    (active state with no child_task_id, the daemon-restart hydration
    case) must also short-circuit BACKED_OFF and dispatch fresh. This is
    the live v7ab/v7ac signature: post-restart, every wave entry has
    dozens of orphans, each previously eating 300s of cooling.
    """
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    assert orch.retry_budget == 2  # carve-out only applies when retry on

    t = _t("u-orphan", "SAL-2603", "ALT-06", 0, "aletheia", size="M",
           estimate=5)
    t.status = TicketStatus.IN_PROGRESS
    # Defining condition for AB-17-y: active state, no child_task_id.
    assert t.child_task_id is None, "fixture invariant"
    t.retry_count = 0
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    back_off_calls: list[str] = []
    real_back_off = orch._back_off_ticket
    def _spy_back_off(ticket):
        back_off_calls.append(ticket.identifier)
        return real_back_off(ticket)
    monkeypatch.setattr(orch, "_back_off_ticket", _spy_back_off)

    # Stuck past the AB-17-y orphan threshold.
    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 120
    )

    updated = await orch._poll_children()

    # Carve-out outcome: PENDING with retry_count untouched.
    assert t.status == TicketStatus.PENDING, (
        f"orphan-active cleanup must short-circuit to PENDING; got "
        f"{t.status}"
    )
    assert t.retry_count == 0, (
        f"orphan-active cleanup must not burn retry budget; got "
        f"retry_count={t.retry_count}"
    )
    assert t.backed_off_at is None
    assert t.child_task_id is None
    assert t.last_failure_reason is None  # cleared post-reset
    assert back_off_calls == [], (
        f"_back_off_ticket must not run for orphan-active cleanup; "
        f"spy saw: {back_off_calls}"
    )
    # The orphan-fail event AND the phantom-reset event ARE both recorded.
    assert any(
        e["kind"] == "ticket_failed"
        and "no_child_task_id" in str(e.get("note", ""))
        for e in orch.state.events
    ), (
        f"expected ticket_failed (no_child_task_id) event; got "
        f"{orch.state.events}"
    )
    reset_evts = [
        e for e in orch.state.events if e["kind"] == "ticket_phantom_reset"
    ]
    assert len(reset_evts) == 1
    assert reset_evts[0]["reason"] == "no_child_task_id"
    assert t in updated


async def test_real_failure_still_routes_through_backed_off(monkeypatch):
    """SAL-2870 phantom carve-out: a REAL failure (silent_complete /
    no_pr_url / mesh-failed / review REQUEST_CHANGES) MUST still go
    through BACKED_OFF + retry_count bump. Phantom carve-out is
    surgically scoped to phantom_child + no_child_task_id only.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    assert orch.retry_budget == 2

    t = _t("u1", "SAL-2870-real", "TIR-01", 0, "tiresias", size="S",
           estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-real-fail-1"
    t.retry_count = 0
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    back_off_calls: list[str] = []
    real_back_off = orch._back_off_ticket
    def _spy_back_off(ticket):
        back_off_calls.append(ticket.identifier)
        return real_back_off(ticket)
    monkeypatch.setattr(orch, "_back_off_ticket", _spy_back_off)

    # Real failure path: child completed without a PR URL (no_pr_url
    # branch). last_failure_reason must NOT be tagged phantom_*.
    mesh.completed_tasks.append({
        "id": "child-real-fail-1",
        "status": "completed",
        "result": {"summary": "did some stuff but no PR opened"},
    })

    await orch._poll_children()

    # Real failure outcome: BACKED_OFF + retry_count++ (legacy SAL-2870).
    assert t.status == TicketStatus.BACKED_OFF, (
        f"real failure must still go through BACKED_OFF; got "
        f"{t.status}"
    )
    assert t.retry_count == 1, (
        f"real failure must increment retry_count; got "
        f"{t.retry_count}"
    )
    assert t.backed_off_at is not None and t.backed_off_at > 0
    assert back_off_calls == [t.identifier], (
        f"_back_off_ticket must run exactly once for real failure; "
        f"spy saw: {back_off_calls}"
    )
    # ticket_backed_off event recorded; no phantom-reset event.
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_backed_off" in kinds
    assert "ticket_phantom_reset" not in kinds, (
        f"real failure must NOT emit ticket_phantom_reset; events: "
        f"{orch.state.events}"
    )


async def test_phantom_carve_out_disabled_when_retry_budget_zero(monkeypatch):
    """SAL-2870 phantom carve-out is gated on retry_budget > 0. With
    retry_budget=0 the operator explicitly disabled retry semantics
    entirely (legacy / tests pinning terminal-FAILED): phantom failures
    must land terminal FAILED in that mode, not PENDING. Carve-out is
    an optimization of the retry path, not a parallel one.
    """
    import time as _time

    from alfred_coo.autonomous_build.orchestrator import (
        STUCK_CHILD_FORCE_FAIL_SEC,
    )

    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2870-zero", "TIR-01", 0, "tiresias", size="S",
           estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-phantom-zero"
    t.retry_budget = 0  # phantom carve-out gated off
    _seed_graph(orch, [t])

    async def _noop_update(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop_update)

    orch._ticket_transition_ts[t.id] = _time.time() - (
        STUCK_CHILD_FORCE_FAIL_SEC + 60
    )

    await orch._poll_children()

    # With retry off, phantom cleanup falls through to terminal FAILED.
    assert t.status == TicketStatus.FAILED, (
        f"retry_budget=0 must keep phantom cleanup terminal-FAILED; "
        f"got {t.status}"
    )
    # No phantom-reset event.
    assert not any(
        e["kind"] == "ticket_phantom_reset" for e in orch.state.events
    ), (
        f"retry_budget=0 must not emit ticket_phantom_reset; events: "
        f"{orch.state.events}"
    )


def test_wave_gate_excuses_escalated():
    """SAL-2886 wave-gate parity: a ticket in ESCALATED is excluded
    from the green-ratio denominator even when it has no human-assigned
    label and no PATH_CONFLICT cache hit. Mirrors _is_wave_gate_excused
    behaviour for PATH_CONFLICT at the per-ticket level.
    """
    orch = _mk_orchestrator()

    t = _t("u1", "SAL-2886-w", "UNMAPPED-99", 0, "tiresias")
    t.status = TicketStatus.ESCALATED
    # No human-assigned label and no _verified_hints cache entry.
    t.labels = []
    orch._verified_hints = {}

    assert orch._is_wave_gate_excused(t) is True, (
        "ESCALATED ticket without human-assigned/PATH_CONFLICT must be "
        "excused via the SAL-2886 fall-through"
    )


def test_envelope_helper_does_not_misclassify_propose_pr_with_side_linear_call():
    """SAL-2886 belt-and-braces: a happy-path envelope with a real
    propose_pr tool call AND a side-effect linear_create_issue (not a
    grounding-gap; e.g. a follow-up to track a flake) must NOT be
    misclassified as the escalate emit. _extract_pr_url returns the URL,
    _envelope_is_grounding_gap returns False, orchestrator takes
    PR_OPEN.
    """
    envelope = {
        "summary": "Opened PR with the fix.",
        "tool_calls": [
            {
                "name": "propose_pr",
                "result": {
                    "pr_url": "https://github.com/salucallc/repo/pull/123",
                    "branch": "fix/sal-2886-side",
                },
            },
            {
                "name": "linear_create_issue",
                "result": {
                    "identifier": "SAL-3001",
                    "title": "track flake in test_foo",
                },
            },
        ],
    }

    # _extract_pr_url surfaces the real PR URL.
    assert (
        AutonomousBuildOrchestrator._extract_pr_url(envelope)
        == "https://github.com/salucallc/repo/pull/123"
    )
    # The grounding-gap discriminator does NOT trigger on a non-gap
    # follow-up linear_create_issue.
    assert (
        AutonomousBuildOrchestrator._envelope_is_grounding_gap(envelope)
        is False
    ), "non-grounding-gap linear_create_issue must not match the escalate discriminator"


# ── SAL-2893: ESCALATED transitions Linear to Done ───────────────────────────
#
# PR #91 (SAL-2886) deliberately left Linear untouched on ESCALATED so the
# operator could inspect the spawned grounding-gap issue. That created a
# downstream stall: every subsequent kickoff's ``build_ticket_graph`` reads
# Linear, sees "In Progress", maps it to ``in_progress``, and AB-17-y catches
# it as an orphan-active 30 min later. Each kickoff burned 30 min on the same
# stale ticket. SAL-2893 fix: transition Linear -> "Done" with a comment
# linking the grounding-gap issue, so the next kickoff sees a terminal state
# and the ticket leaves the active loop entirely. The grounding-gap link in
# the comment carries the audit trail forward without spelunking soul memory.


async def test_escalated_transitions_linear_to_done_with_grounding_gap_link(
    monkeypatch,
):
    """SAL-2893 happy-path: when ``_poll_children`` classifies an envelope
    as the SAL-2886 escalate emit, it MUST transition Linear to "Done"
    AND post a comment whose body contains the grounding-gap issue's
    identifier so the operator can navigate to it from the parent ticket.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2893-x", "TIR-01", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-2893-x"
    t.linear_state = "In Progress"  # the stale state SAL-2893 fixes
    _seed_graph(orch, [t])

    linear_calls: list[tuple[str, str]] = []

    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    comment_calls: list[tuple[str, str]] = []

    async def _fake_comment(ticket, grounding_gap_ident):
        comment_calls.append((ticket.identifier, grounding_gap_ident))

    monkeypatch.setattr(
        orch, "_post_escalated_linear_comment", _fake_comment
    )

    mesh.completed_tasks.append({
        "id": "child-2893-x",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2893-x ...",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "linear_create_issue",
                "result": {
                    "identifier": "SAL-2999",
                    "title": "grounding gap: SAL-2893-x missing target",
                    "url": "https://linear.app/saluca/issue/SAL-2999",
                },
            }],
            "summary": "Escalated due to PATH_CONFLICT on already-merged target",
        },
    })

    await orch._poll_children()

    assert t.status == TicketStatus.ESCALATED, (
        f"sanity: discriminator must classify as ESCALATED, got {t.status}"
    )
    # Linear MUST receive a Done transition (the SAL-2893 fix).
    assert ("SAL-2893-x", "Done") in linear_calls, (
        f"SAL-2893: ESCALATED must transition Linear to Done; got {linear_calls}"
    )
    # And NOT Backlog — the SAL-2886 negative invariant still holds.
    assert ("SAL-2893-x", "Backlog") not in linear_calls, (
        f"escalate path must NOT roll Linear back to Backlog: {linear_calls}"
    )
    # Comment helper invoked exactly once with the grounding-gap identifier.
    assert comment_calls == [("SAL-2893-x", "SAL-2999")], (
        f"escalated comment helper not invoked correctly: {comment_calls}"
    )
    # ticket.linear_state was updated locally to mirror the Linear write so
    # the next tick / restore sees the new state without a Linear re-read.
    assert t.linear_state == "Done", (
        f"ticket.linear_state must be updated to Done locally: {t.linear_state}"
    )


async def test_escalated_does_not_transition_if_already_done(monkeypatch):
    """SAL-2893 idempotency guard: if Linear is already "Done" on entry to
    the ESCALATED branch (e.g. operator manually closed it, or this is a
    re-entry from rehydrated state), the orchestrator must NOT call
    ``_update_linear_state`` again and must NOT post a duplicate comment.
    The ticket still transitions to ``TicketStatus.ESCALATED`` and still
    records the ``ticket_escalated`` event.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2893-y", "TIR-02", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-2893-y"
    t.linear_state = "Done"  # already there — the idempotency case
    _seed_graph(orch, [t])

    linear_calls: list[tuple[str, str]] = []

    async def _fake_update(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    comment_calls: list[tuple[str, str]] = []

    async def _fake_comment(ticket, grounding_gap_ident):
        comment_calls.append((ticket.identifier, grounding_gap_ident))

    monkeypatch.setattr(
        orch, "_post_escalated_linear_comment", _fake_comment
    )

    mesh.completed_tasks.append({
        "id": "child-2893-y",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "linear_create_issue",
                "result": {
                    "identifier": "SAL-3000",
                    "title": "grounding gap: SAL-2893-y duplicate",
                },
            }],
        },
    })

    await orch._poll_children()

    # Status transition still happens — ESCALATED is the orchestrator's
    # internal terminal regardless of Linear state.
    assert t.status == TicketStatus.ESCALATED
    # Event still recorded with the gap identifier (audit trail intact).
    escalated_events = [
        e for e in orch.state.events if e["kind"] == "ticket_escalated"
    ]
    assert len(escalated_events) == 1
    assert escalated_events[0]["grounding_gap"] == "SAL-3000"
    # Idempotency: no Linear write, no comment.
    assert linear_calls == [], (
        f"idempotency violated: Linear must not be re-written when already "
        f"Done; got {linear_calls}"
    )
    assert comment_calls == [], (
        f"idempotency violated: comment must not be re-posted when already "
        f"Done; got {comment_calls}"
    )


async def test_grounding_gap_link_in_done_comment_matches_record_event_id(
    monkeypatch,
):
    """SAL-2893 audit-trail invariant: the grounding-gap identifier
    embedded in the Linear "Done" comment body MUST be byte-identical to
    the ``grounding_gap`` value recorded in the ``ticket_escalated`` soul
    event for the same tick. This is what lets a human follow the
    operator-readable Linear comment back to the soul-memory event log
    without ambiguity.

    Drives the real ``_post_escalated_linear_comment`` (no monkeypatch on
    that helper) but stubs the underlying ``linear_add_comment`` tool spec
    so we can capture the body string.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    t = _t("u1", "SAL-2893-z", "TIR-03", 0, "tiresias", size="S", estimate=1)
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-2893-z"
    t.linear_state = "In Progress"
    _seed_graph(orch, [t])

    async def _fake_update(ticket, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _fake_update)

    # Inject a fake ``linear_add_comment`` spec into BUILTIN_TOOLS for
    # this test — the real tool isn't registered yet and the helper has
    # a defensive ``if spec is None: return`` short-circuit. Restore the
    # original entry on teardown via monkeypatch.setitem so other tests
    # don't see our stub.
    from alfred_coo import tools as _tools

    captured: dict = {}

    async def _spy_handler(*, issue_id, body):
        captured["issue_id"] = issue_id
        captured["body"] = body
        return {"ok": True}

    class _StubSpec:
        handler = staticmethod(_spy_handler)

    monkeypatch.setitem(_tools.BUILTIN_TOOLS, "linear_add_comment", _StubSpec)

    expected_gap = "SAL-3001"
    mesh.completed_tasks.append({
        "id": "child-2893-z",
        "status": "completed",
        "result": {
            "tool_calls": [{
                "name": "linear_create_issue",
                "result": {
                    "identifier": expected_gap,
                    "title": "grounding gap: SAL-2893-z merged in prior wave",
                    "url": f"https://linear.app/saluca/issue/{expected_gap}",
                },
            }],
        },
    })

    await orch._poll_children()

    # The recorded event's grounding_gap (the "record_event_id" the spec
    # refers to) is the source of truth.
    escalated_events = [
        e for e in orch.state.events if e["kind"] == "ticket_escalated"
    ]
    assert len(escalated_events) == 1
    recorded_gap = escalated_events[0]["grounding_gap"]
    assert recorded_gap == expected_gap

    # The comment was posted against the parent ticket UUID (NOT the gap
    # identifier — comments attach to the parent so a Linear viewer
    # following the parent thread sees the link).
    assert captured.get("issue_id") == t.id
    body = captured.get("body") or ""
    # The grounding-gap identifier MUST appear verbatim in the body so
    # the human can copy/click straight to it.
    assert recorded_gap in body, (
        f"comment body missing grounding-gap id {recorded_gap!r}: {body!r}"
    )
    # And the link form (a real Linear URL) must also be present.
    assert f"https://linear.app/saluca/issue/{recorded_gap}" in body, (
        f"comment body missing grounding-gap URL: {body!r}"
    )



# ── AB-17-v · dispatch-side human-assigned skip ────────────────────────────


async def test_dispatch_skips_human_assigned_ticket(monkeypatch):
    """AB-17-v: a ticket carrying the ``human-assigned`` label MUST NOT be
    passed to ``_dispatch_child``. Wave-gate already excludes such tickets
    from the green-ratio denominator (``_is_wave_gate_excused``), but the
    dispatch path historically ran builders against them anyway, producing
    stub PRs that flipped tickets Done in error (2026-04-27 incident:
    SAL-2641, SAL-2647, +4 phantom flips).

    Mirrors the existing label predicate in ``_is_wave_gate_excused`` so the
    two sides agree on what "human-assigned" means. The dispatch-skip path
    sets ``ticket.status = ESCALATED`` directly so the wave-loop exit
    condition (all wave_tickets in TERMINAL_STATES) fires cleanly without
    any external terminalisation.
    """
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    orch.max_parallel_subs = 4

    # One ticket the orchestrator SHOULD dispatch, one labelled human-assigned
    # that it MUST skip. Both PENDING, same wave, same epic.
    auto = _t("ua", "SAL-A", "TIR-01", 0, "tiresias")
    human = _t(
        "uh", "SAL-H", "TIR-02", 0, "tiresias",
        labels=["human-assigned"],
    )
    _seed_graph(orch, [auto, human])

    # Stub the surrounding tick machinery.
    async def _noop(*a, **kw):
        return None

    async def _noop_list(*a, **kw):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_children", _noop_list)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    monkeypatch.setattr(orch, "_check_cancel_signal", _noop)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )

    # Spy on _dispatch_child. Capture what was dispatched and flip the
    # auto ticket terminal so the wave-loop exit condition fires.
    dispatched: list[Ticket] = []

    async def _spy_dispatch(ticket):
        dispatched.append(ticket)
        ticket.child_task_id = f"child-{ticket.identifier}"
        ticket.status = TicketStatus.MERGED_GREEN

    monkeypatch.setattr(orch, "_dispatch_child", _spy_dispatch)

    # Bound the loop in case of a regression.
    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] > 10:
            raise RuntimeError(
                "dispatch loop did not converge within 10 ticks"
            )
        await real_sleep(0)

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    # The auto ticket was dispatched; the human-assigned ticket was NOT.
    dispatched_idents = [t.identifier for t in dispatched]
    assert "SAL-A" in dispatched_idents, (
        f"auto-ticket should have been dispatched; got {dispatched_idents!r}"
    )
    assert "SAL-H" not in dispatched_idents, (
        "human-assigned ticket must NOT be dispatched; "
        f"got {dispatched_idents!r}"
    )
    # The dispatch-skip path must terminalise the human-assigned ticket
    # itself (status=ESCALATED) so the wave loop's exit condition fires.
    # Without this, _dispatch_wave hangs forever on any wave that contains
    # a human-assigned ticket because PENDING is not in TERMINAL_STATES.
    assert human.status == TicketStatus.ESCALATED, (
        f"dispatch-skip must set status=ESCALATED for terminal-success "
        f"accounting; got {human.status!r}"
    )


async def test_dispatch_skips_human_assigned_label_case_insensitive(monkeypatch):
    """AB-17-v: label match is case-insensitive (mirrors
    ``_is_wave_gate_excused``). The skip path also sets status=ESCALATED
    so a wave containing only human-assigned tickets exits cleanly."""
    orch = _mk_orchestrator()
    orch.poll_sleep_sec = 0
    orch.max_parallel_subs = 4

    human = _t(
        "uh", "SAL-H", "TIR-02", 0, "tiresias",
        labels=["Human-Assigned"],
    )
    _seed_graph(orch, [human])

    async def _noop(*a, **kw):
        return None

    async def _noop_list(*a, **kw):
        return []

    monkeypatch.setattr(orch, "_mark_repo_missing_tickets", _noop)
    monkeypatch.setattr(orch, "_poll_children", _noop_list)
    monkeypatch.setattr(orch, "_poll_reviews", _noop_list)
    monkeypatch.setattr(orch, "_check_budget", _noop)
    monkeypatch.setattr(orch, "_status_tick", _noop)
    monkeypatch.setattr(orch, "_stall_watcher", _noop)
    monkeypatch.setattr(orch, "_check_cancel_signal", _noop)
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.checkpoint", _noop
    )

    dispatched: list[Ticket] = []

    async def _spy_dispatch(ticket):
        dispatched.append(ticket)
        ticket.status = TicketStatus.MERGED_GREEN

    monkeypatch.setattr(orch, "_dispatch_child", _spy_dispatch)

    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        ticks["n"] += 1
        if ticks["n"] > 5:
            raise RuntimeError("loop did not exit on empty dispatchable set")
        await real_sleep(0)

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep",
        counting_sleep,
    )

    await asyncio.wait_for(orch._dispatch_wave(0), timeout=2.0)

    assert dispatched == [], (
        f"Mixed-case 'Human-Assigned' label must still skip dispatch; "
        f"got {[t.identifier for t in dispatched]!r}"
    )
    assert human.status == TicketStatus.ESCALATED, (
        f"dispatch-skip must set status=ESCALATED on case-insensitive "
        f"label match; got {human.status!r}"
    )
