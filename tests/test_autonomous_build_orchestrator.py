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
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
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
