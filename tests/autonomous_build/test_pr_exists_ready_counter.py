"""PR-exists-skip ready-counter / AWAITING_REVIEW tests (Gap 3, 2026-04-29).

Coverage for the AWAITING_REVIEW bookkeeping bucket added to
``TicketStatus``: tickets whose PRs have been handed off to a Hawkman QA
review task (either by the builder->PR transition path or by the
inherited-PR ``_fire_review_for_inherited_pr`` helper) sit in
AWAITING_REVIEW, which is excluded from BOTH the ``_select_ready``
candidate set AND ``ACTIVE_TICKET_STATES`` (in_flight). That lets the
wave deadlock-grace timer arm cleanly when a wave is purely awaiting
Hawkman verdicts; previously the PR-exists-skip path left tickets in
PENDING and the deadlock-grace counter never reached zero.

Pins six behaviours:
  1. ``_fire_review_for_inherited_pr`` flips the ticket to
     AWAITING_REVIEW after registering an existing review task.
  2. ``_select_ready`` excludes AWAITING_REVIEW tickets.
  3. ``_in_flight_for_wave`` excludes AWAITING_REVIEW tickets, so the
     deadlock-grace arm condition (``in_flight=0 + ready=0``) holds even
     when AWAITING_REVIEW tickets are present.
  4. ``_poll_reviews`` transitions AWAITING_REVIEW -> MERGED_GREEN on
     APPROVE.
  5. ``_poll_reviews`` transitions AWAITING_REVIEW -> DISPATCHED on
     REQUEST_CHANGES (the respawn path's terminal state for the verdict).
  6. The builder->PR transition path inside ``_poll_children`` lands the
     ticket in AWAITING_REVIEW (regression: prior to Gap 3 it landed in
     REVIEWING which was both in_flight AND polled by ``_poll_reviews``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (mirror test_inherited_open_pr_review_dispatch.py) ──────────────


class _FakeMesh:
    def __init__(self, pending=None, claimed=None, completed=None):
        self.created: list[dict] = []
        self.pending = list(pending or [])
        self.claimed = list(claimed or [])
        self.completed = list(completed or [])
        self._next_id = 1

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {
            "title": title, "description": description,
            "from_session_id": from_session_id,
        }
        self.created.append(rec)
        nid = f"review-task-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        if status == "pending":
            return list(self.pending)
        if status == "claimed":
            return list(self.claimed)
        if status == "completed":
            return list(self.completed)
        if status == "failed":
            return []
        return []


class _FakeSoul:
    def __init__(self):
        self.writes: list[dict] = []

    async def write_memory(self, content, topics=None):
        self.writes.append({"content": content, "topics": topics or []})
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        return []


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


class _FakePersona:
    name = "autonomous-build-a"
    handler = "AutonomousBuildOrchestrator"


def _mk_orch(mesh=None, soul=None) -> AutonomousBuildOrchestrator:
    task = {
        "id": "kick-gap3",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": "",
    }
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_FakePersona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _t(uuid: str, ident: str, **kwargs) -> Ticket:
    return Ticket(
        id=uuid,
        identifier=ident,
        code=kwargs.pop("code", "OPS-01"),
        title=f"{ident} test",
        wave=kwargs.pop("wave", 0),
        epic=kwargs.pop("epic", "ops"),
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        **kwargs,
    )


def _seed_graph(orch: AutonomousBuildOrchestrator, tickets: list[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


# ── 1. _fire_review_for_inherited_pr -> AWAITING_REVIEW ───────────────────


@pytest.mark.asyncio
async def test_inherited_pr_existing_mesh_task_marks_awaiting_review(monkeypatch):
    """When a Hawkman QA task already exists on the mesh, the helper
    registers it AND flips ticket.status = AWAITING_REVIEW so the
    dispatch loop's ready+in_flight buckets exclude this ticket on the
    next tick.
    """
    mesh = _FakeMesh(
        claimed=[
            {
                "id": "pre-existing-rev",
                "title": (
                    "[persona:hawkman-qa-a] [wave-2] [tiresias] "
                    "review SAL-3243 OPS-09 (cycle #1)"
                ),
                "status": "claimed",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=2, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    dispatch_review_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    fired_id = await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    assert fired_id == "pre-existing-rev"
    assert t.status == TicketStatus.AWAITING_REVIEW
    dispatch_review_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_inherited_pr_fresh_dispatch_marks_awaiting_review(monkeypatch):
    """When no existing Hawkman QA task is present, the helper fires a
    fresh ``_dispatch_review`` AND flips the ticket to AWAITING_REVIEW
    only after the dispatch lands a review_task_id in state. A fresh
    fire that fails leaves the ticket in PENDING (regression: caller
    falls back to original skip-only behaviour).
    """
    mesh = _FakeMesh()  # empty: no pending/claimed reviews
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=2, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    async def _fake_dispatch(ticket):
        orch.state.review_task_ids[ticket.id] = "fresh-review-1"

    monkeypatch.setattr(orch, "_dispatch_review", _fake_dispatch)

    fired_id = await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    assert fired_id == "fresh-review-1"
    assert t.status == TicketStatus.AWAITING_REVIEW


@pytest.mark.asyncio
async def test_inherited_pr_dispatch_failure_leaves_pending(monkeypatch):
    """If ``_dispatch_review`` raises (mesh outage), the helper must
    swallow the exception AND leave the ticket in its original PENDING
    state. The dispatch loop will retry on the next tick.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=2, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    dispatch_review_mock = AsyncMock(side_effect=RuntimeError("mesh down"))
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    fired_id = await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    assert fired_id is None
    assert t.status == TicketStatus.PENDING


# ── 2. _select_ready excludes AWAITING_REVIEW ─────────────────────────────


def test_select_ready_excludes_awaiting_review_tickets():
    """``_select_ready`` only returns PENDING / BLOCKED tickets. An
    AWAITING_REVIEW ticket must not be surfaced as ready, otherwise the
    dispatch loop would re-iterate the PR-exists-skip branch indefinitely
    AND the deadlock-grace counter would never reach zero.
    """
    orch = _mk_orch()

    t_pending = _t("u-1", "SAL-1", wave=2)
    t_pending.status = TicketStatus.PENDING

    t_awaiting = _t("u-2", "SAL-2", wave=2)
    t_awaiting.status = TicketStatus.AWAITING_REVIEW

    _seed_graph(orch, [t_pending, t_awaiting])

    ready = orch._select_ready([t_pending, t_awaiting], in_flight=[])

    ready_ids = {t.id for t in ready}
    assert "u-1" in ready_ids
    assert "u-2" not in ready_ids


# ── 3. _in_flight_for_wave excludes AWAITING_REVIEW (deadlock-grace arms) ─


def test_in_flight_for_wave_excludes_awaiting_review():
    """``_in_flight_for_wave`` must exclude AWAITING_REVIEW so the
    deadlock-grace arm condition (in_flight=0 + ready=0) can hold even
    when a wave has many AWAITING_REVIEW tickets. This is the central
    Gap 3 fix: previously REVIEWING (which IS in ACTIVE_TICKET_STATES)
    held the in_flight count high and grace never armed.
    """
    orch = _mk_orch()

    t_in_progress = _t("u-1", "SAL-1", wave=2)
    t_in_progress.status = TicketStatus.IN_PROGRESS  # active

    t_awaiting_a = _t("u-2", "SAL-2", wave=2)
    t_awaiting_a.status = TicketStatus.AWAITING_REVIEW

    t_awaiting_b = _t("u-3", "SAL-3", wave=2)
    t_awaiting_b.status = TicketStatus.AWAITING_REVIEW

    t_reviewing = _t("u-4", "SAL-4", wave=2)
    t_reviewing.status = TicketStatus.REVIEWING  # still active for legacy

    _seed_graph(orch, [t_in_progress, t_awaiting_a, t_awaiting_b, t_reviewing])

    in_flight = orch._in_flight_for_wave(2)
    in_flight_ids = {t.id for t in in_flight}

    # Active states only: IN_PROGRESS + REVIEWING. AWAITING_REVIEW must
    # NOT count as in-flight.
    assert "u-1" in in_flight_ids
    assert "u-4" in in_flight_ids
    assert "u-2" not in in_flight_ids
    assert "u-3" not in in_flight_ids
    assert len(in_flight) == 2


def test_deadlock_grace_arm_condition_with_awaiting_review_only():
    """End-to-end pin: a wave with ONLY AWAITING_REVIEW tickets reports
    in_flight=0 AND ready=0, so the deadlock-grace arm predicate
    ``not in_flight and not ready`` evaluates True. Mirrors the production
    dispatch loop logic at orchestrator.py:3739 without invoking the
    full async dispatch loop.
    """
    orch = _mk_orch()

    t_a = _t("u-1", "SAL-1", wave=2)
    t_a.status = TicketStatus.AWAITING_REVIEW
    t_b = _t("u-2", "SAL-2", wave=2)
    t_b.status = TicketStatus.AWAITING_REVIEW
    t_c = _t("u-3", "SAL-3", wave=2)
    t_c.status = TicketStatus.AWAITING_REVIEW

    _seed_graph(orch, [t_a, t_b, t_c])

    wave_tickets = orch.graph.tickets_in_wave(2)
    in_flight = orch._in_flight_for_wave(2)
    ready = orch._select_ready(wave_tickets, in_flight)

    assert len(in_flight) == 0
    assert len(ready) == 0
    # Predicate from orchestrator.py:3739 — `not in_flight and not ready`.
    assert (not in_flight) and (not ready)


# ── 4. _poll_reviews: AWAITING_REVIEW -> MERGED_GREEN on APPROVE ──────────


@pytest.mark.asyncio
async def test_poll_reviews_awaiting_review_to_merged_green_on_approve(
    monkeypatch,
):
    """A ticket in AWAITING_REVIEW with a completed review whose verdict
    is APPROVE transitions through MERGE_REQUESTED to MERGED_GREEN
    (assuming ``_merge_pr`` returns True). Pins that ``_poll_reviews``
    treats AWAITING_REVIEW as a polled state on par with REVIEWING.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-1", "SAL-1", code="TIR-01", wave=2, epic="tiresias")
    t.status = TicketStatus.AWAITING_REVIEW
    t.pr_url = "https://github.com/salucallc/foo/pull/100"
    t.review_task_id = "review-1"
    t.child_task_id = "child-1"
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    # Stage a completed review record carrying APPROVE.
    orch._last_completed_by_id = {
        "review-1": {
            "id": "review-1",
            "status": "completed",
            "result": {
                "tool_calls": [
                    {
                        "name": "pr_review",
                        "result": {"state": "APPROVE"},
                    },
                ],
            },
        },
    }

    # Stub merge to succeed and Linear writes to noop.
    async def _fake_merge(ticket):
        orch.state.merged_pr_urls[ticket.id] = "deadbeef"
        return True
    monkeypatch.setattr(orch, "_merge_pr", _fake_merge)

    async def _noop(*a, **kw): return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    # Stub destructive guardrail to no-op (fail-open for tests).
    async def _no_guardrail(ticket):
        from alfred_coo.autonomous_build.destructive_guardrail import (
            GuardrailResult,
        )
        return GuardrailResult(tripped=False)
    monkeypatch.setattr(
        orch,
        "_check_destructive_guardrail_for_ticket",
        _no_guardrail,
    )

    updated = await orch._poll_reviews()

    assert t in updated
    assert t.status == TicketStatus.MERGED_GREEN


# ── 5. _poll_reviews: AWAITING_REVIEW -> DISPATCHED on REQUEST_CHANGES ────


@pytest.mark.asyncio
async def test_poll_reviews_awaiting_review_to_dispatched_on_request_changes(
    monkeypatch,
):
    """A ticket in AWAITING_REVIEW with a completed review whose verdict
    is REQUEST_CHANGES (and review_cycles under cap) is respawned via
    ``_respawn_child_with_fixes`` and lands in DISPATCHED — the
    orchestrator's "back to active dispatch" terminal for the verdict
    handler. Pins that AWAITING_REVIEW is the input state for fix-round
    respawn flow, mirroring the existing REVIEWING path.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-1", "SAL-1", code="TIR-01", wave=2, epic="tiresias")
    t.status = TicketStatus.AWAITING_REVIEW
    t.pr_url = "https://github.com/salucallc/foo/pull/100"
    t.review_task_id = "review-1"
    t.child_task_id = "child-1"
    t.review_cycles = 0
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    orch._last_completed_by_id = {
        "review-1": {
            "id": "review-1",
            "status": "completed",
            "result": {
                "summary": "Please make these changes: REQUEST_CHANGES",
                "tool_calls": [],
            },
        },
    }

    respawn_called = {"n": 0}

    async def _fake_respawn(ticket, body):
        respawn_called["n"] += 1
        ticket.child_task_id = "child-respawn-1"

    monkeypatch.setattr(orch, "_respawn_child_with_fixes", _fake_respawn)

    async def _noop(*a, **kw): return None
    monkeypatch.setattr(orch, "_update_linear_state", _noop)

    updated = await orch._poll_reviews()

    assert t in updated
    assert t.status == TicketStatus.DISPATCHED
    assert respawn_called["n"] == 1
    assert t.review_cycles == 1
    # Stale review pointer cleared so a fresh PR_OPEN seeds a new round.
    assert t.review_task_id is None
    assert t.id not in orch.state.review_task_ids


# ── 6. Builder->PR transition path lands in AWAITING_REVIEW ───────────────


@pytest.mark.asyncio
async def test_builder_to_pr_transition_lands_in_awaiting_review(monkeypatch):
    """Regression: when ``_poll_children`` sees a completed builder
    child carrying a PR URL, it must land the ticket in AWAITING_REVIEW
    (not REVIEWING) after dispatching the review task. This is the
    central post-Gap-3 behaviour — pre-Gap-3 the ticket landed in
    REVIEWING which counted toward in_flight.
    """
    mesh = _FakeMesh(
        completed=[
            {
                "id": "child-builder-1",
                "title": (
                    "[persona:alfred-coo-a] [wave-0] [ops] "
                    "SAL-2999 OPS-01 build"
                ),
                "status": "completed",
                "result": {
                    "summary": (
                        "Opened PR https://github.com/salucallc/foo/"
                        "pull/100"
                    ),
                    "follow_up_tasks": [],
                    "tool_calls": [],
                },
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)

    t = _t("u-2999", "SAL-2999", code="OPS-01")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-builder-1"
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap3")

    dispatch_review_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    async def _fake_update_linear(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update_linear)

    await orch._poll_children()

    dispatch_review_mock.assert_awaited_once()
    assert t.pr_url == "https://github.com/salucallc/foo/pull/100"
    assert t.status == TicketStatus.AWAITING_REVIEW
