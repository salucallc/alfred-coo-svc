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
    HintStatus,
    PathResult,
    TargetHint,
    VerificationResult,
    _TARGET_HINTS,
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
    """AB-17-a is data correctness only: still 16 entries after the fix."""
    assert len(_TARGET_HINTS) == 16


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
