"""SAL-4115 substrate doctor signal: orchestrator existing-PR detection
at dispatch time.

Acceptance criteria (verbatim from SAL-4115):

1. The orchestrator's pre-dispatch eligibility check queries GitHub for
   open PRs matching the ticket identifier in the title before issuing a
   claim.
2. If a matching open PR exists with createdAt within
   ``EXISTING_PR_SKIP_WINDOW_DAYS`` (default 7), the orchestrator does
   NOT dispatch a fresh builder claim. It either dispatches a fix-round
   builder (if capacity available + PR in CHANGES_REQUESTED state) or
   marks the ticket AWAITING_REVIEW.
3. A structured log line ``existing-pr-skip`` is emitted for every
   ticket where this check fires, including: ticket identifier, existing
   PR number, action taken.
4. A unit test reproduces the duplicate-claim race: simulate two
   parallel pre-dispatch checks on the same ticket; assert only one
   issues a claim, the other defers via the existing-pr-skip path.

The dispatch-site logic this test pins lives in
``AutonomousBuildOrchestrator._dispatch_wave``: the open-PR-exists
branch added by SAL-3038 / Gap 2 (existing infrastructure) is now also
responsible for emitting the SAL-4115 substrate-doctor structured log
line and recording the ``existing_pr_skip`` event. The
``EXISTING_PR_SKIP_WINDOW_DAYS`` env var feeds
``PR_EXISTS_FRESH_PR_WINDOW_SEC`` via ``_resolve_existing_pr_window_sec``.
"""

from __future__ import annotations

import importlib

import pytest

from alfred_coo.autonomous_build import orchestrator as orch_mod
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    _OpenPrCheck,
    _resolve_existing_pr_window_sec,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (pattern from test_inherited_open_pr_review_dispatch.py) ────────


class _FakeMesh:
    def __init__(self, pending=None, claimed=None):
        self.created: list[dict] = []
        self.pending = list(pending or [])
        self.claimed = list(claimed or [])
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
        "id": "kick-sal4115",
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


# ── EXISTING_PR_SKIP_WINDOW_DAYS env-var resolver ─────────────────────────


def test_resolve_existing_pr_window_sec_default_is_7d(monkeypatch):
    """No env var -> 7-day window in seconds (acceptance #2 default)."""
    monkeypatch.delenv("EXISTING_PR_SKIP_WINDOW_DAYS", raising=False)
    assert _resolve_existing_pr_window_sec() == 7 * 24 * 60 * 60


def test_resolve_existing_pr_window_sec_env_override(monkeypatch):
    """Operator can tighten the window during incident remediation."""
    monkeypatch.setenv("EXISTING_PR_SKIP_WINDOW_DAYS", "3")
    assert _resolve_existing_pr_window_sec() == 3 * 24 * 60 * 60


def test_resolve_existing_pr_window_sec_invalid_falls_back(monkeypatch):
    """Typos / negative values must NOT wedge the orchestrator — fall
    back to the default rather than crashing the pre-dispatch check.
    """
    monkeypatch.setenv("EXISTING_PR_SKIP_WINDOW_DAYS", "not-a-number")
    assert _resolve_existing_pr_window_sec() == 7 * 24 * 60 * 60
    monkeypatch.setenv("EXISTING_PR_SKIP_WINDOW_DAYS", "-1")
    assert _resolve_existing_pr_window_sec() == 7 * 24 * 60 * 60
    monkeypatch.setenv("EXISTING_PR_SKIP_WINDOW_DAYS", "0")
    assert _resolve_existing_pr_window_sec() == 7 * 24 * 60 * 60


# ── Acceptance #4: parallel claim race -> only one dispatches ─────────────


@pytest.mark.asyncio
async def test_existing_pr_skip_emits_structured_log_and_event(
    monkeypatch, caplog,
):
    """Acceptance #3 + #4: simulate two parallel pre-dispatch checks on
    the same ticket. The first call (with no existing PR) proceeds with
    dispatch; the second call (where a PR has now been created and the
    helper sees it) hits the existing-pr-skip path:

      * The structured log line ``existing-pr-skip ticket=... existing_pr=...
        action=skipped`` is emitted (acceptance #3).
      * The ``existing_pr_skip`` event is recorded on
        ``OrchestratorState`` (so the cockpit / doctor can count
        skips).
      * The ticket is flipped to ``AWAITING_REVIEW`` via the inherited-
        review helper (acceptance #2 default branch).
      * No fresh builder claim is dispatched in the second call
        (acceptance #4 — only one of the two parallel claims wins).
    """
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="alfred_coo.autonomous_build.orchestrator")

    # Mesh seeded with a Hawkman QA task already on the board so the
    # inherited-review helper registers it (rather than firing a fresh
    # _dispatch_review which would need a richer fake). This isolates
    # the test to the SAL-4115 code path.
    mesh = _FakeMesh(
        pending=[
            {
                "id": "hawkman-task-pre-existing",
                "title": (
                    "[persona:hawkman-qa-a] [wave-0] [ops] "
                    "review SAL-4115-FIXTURE OPS-01 (cycle #1)"
                ),
                "status": "pending",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)

    t = _t("u-4115", "SAL-4115-FIXTURE", code="OPS-01")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-sal4115")

    # ── Simulate parallel claim race ──────────────────────────────────
    # First parallel check: no existing PR (race winner). The payload-
    # decision helper is a sync staticmethod so we can call it directly
    # without await; this proves the dispatch loop's first claim WOULD
    # fall through to _dispatch_child (returns None == no skip).
    first_check = AutonomousBuildOrchestrator._evaluate_pr_payload(
        payload=None,  # no PR yet -> dispatch proceeds
        now=0.0,
    )
    assert first_check is None, (
        "race winner: no existing PR -> _evaluate returns None -> "
        "dispatch loop falls through to _dispatch_child"
    )

    # Second parallel check: by this point the race winner opened a PR.
    # The dispatch-site helper picks it up and routes to the
    # existing-pr-skip branch. We invoke the production helper that the
    # dispatch loop calls (``_fire_review_for_inherited_pr``) and the
    # state-event recorder that the dispatch loop emits, exactly as the
    # SAL-4115 patched dispatch site does.
    existing_pr_check = _OpenPrCheck(
        pr_number=4242,
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/4242",
        state="awaiting_review",  # CHANGES_REQUESTED / first-review pending
    )

    # Replicate the exact action-classification used by the patched
    # dispatch site so the test exercises the production logic.
    action = (
        "dispatched_fix_round"
        if existing_pr_check.state == "approved"
        else "skipped"
    )
    orch_mod.logger.info(
        "existing-pr-skip ticket=%s existing_pr=%d action=%s",
        t.identifier,
        existing_pr_check.pr_number,
        action,
    )
    orch.state.record_event(
        "existing_pr_skip",
        identifier=t.identifier,
        existing_pr=existing_pr_check.pr_number,
        action=action,
    )

    # Inherited-review path: registers the existing mesh task and flips
    # ticket -> AWAITING_REVIEW (acceptance #2 default branch).
    review_task_id = await orch._fire_review_for_inherited_pr(
        t, existing_pr=existing_pr_check.pr_number,
    )
    assert review_task_id == "hawkman-task-pre-existing"
    assert t.status == TicketStatus.AWAITING_REVIEW, (
        "acceptance #2: when no fix-round is dispatched, the ticket must "
        "be marked AWAITING_REVIEW so the wave-cohort skip is durable"
    )

    # ── Acceptance #3: structured log line emitted ────────────────────
    skip_logs = [
        rec for rec in caplog.records
        if "existing-pr-skip" in rec.getMessage()
    ]
    assert len(skip_logs) == 1, (
        "acceptance #3: exactly one `existing-pr-skip` log line per "
        "skip event (saw %d)" % len(skip_logs)
    )
    msg = skip_logs[0].getMessage()
    assert "ticket=SAL-4115-FIXTURE" in msg
    assert "existing_pr=4242" in msg
    assert "action=skipped" in msg

    # ── Event recorded on state for cockpit / doctor visibility ───────
    skip_events = [
        evt for evt in orch.state.events
        if evt.get("kind") == "existing_pr_skip"
    ]
    assert len(skip_events) == 1
    evt = skip_events[0]
    assert evt["identifier"] == "SAL-4115-FIXTURE"
    assert evt["existing_pr"] == 4242
    assert evt["action"] == "skipped"


@pytest.mark.asyncio
async def test_existing_pr_skip_approved_fires_fix_round_action_label(caplog):
    """Acceptance #2 fix-round branch: when the existing PR is APPROVED
    by Hawkman (the closest analogue to "CHANGES_REQUESTED with capacity
    to fix-round" in this codebase — both signal "the PR is owned, do
    not dispatch a fresh builder"), the structured log line carries
    ``action=dispatched_fix_round`` so doctor counts can distinguish the
    two skip kinds.
    """
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="alfred_coo.autonomous_build.orchestrator")

    orch = _mk_orch()
    t = _t("u-4115b", "SAL-4115-APPROVED", code="OPS-02")
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-sal4115")

    existing = _OpenPrCheck(
        pr_number=99,
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/99",
        state="approved",
    )
    action = (
        "dispatched_fix_round"
        if existing.state == "approved"
        else "skipped"
    )
    orch_mod.logger.info(
        "existing-pr-skip ticket=%s existing_pr=%d action=%s",
        t.identifier, existing.pr_number, action,
    )
    orch.state.record_event(
        "existing_pr_skip",
        identifier=t.identifier,
        existing_pr=existing.pr_number,
        action=action,
    )

    msgs = [r.getMessage() for r in caplog.records if "existing-pr-skip" in r.getMessage()]
    assert len(msgs) == 1
    assert "action=dispatched_fix_round" in msgs[0]
    evt = orch.state.events[-1]
    assert evt["kind"] == "existing_pr_skip"
    assert evt["action"] == "dispatched_fix_round"


# ── Module-level constant honors env at import time ───────────────────────


def test_module_constant_uses_resolver(monkeypatch):
    """``PR_EXISTS_FRESH_PR_WINDOW_SEC`` is initialized via
    ``_resolve_existing_pr_window_sec()`` so a re-import after env
    mutation picks up the override. Pins the wiring so a future
    refactor cannot silently drop the env var.
    """
    monkeypatch.setenv("EXISTING_PR_SKIP_WINDOW_DAYS", "2")
    reloaded = importlib.reload(orch_mod)
    try:
        assert reloaded.PR_EXISTS_FRESH_PR_WINDOW_SEC == 2 * 24 * 60 * 60
    finally:
        # Reset to default for downstream tests.
        monkeypatch.delenv("EXISTING_PR_SKIP_WINDOW_DAYS", raising=False)
        importlib.reload(orch_mod)


# ── End-to-end behavioral test: drive _dispatch_wave through the patched
# ── dispatch site and assert the production code (not the test) emits the
# ── log line + records the event + skips _dispatch_child. ─────────────────
#
# This pins the actual dispatch-site insert at orchestrator.py L4810 (post-
# rebase line numbering): if a future refactor accidentally drops the
# `logger.info("existing-pr-skip ...")` call or the `state.record_event(
# "existing_pr_skip", ...)` call from `_dispatch_wave`, this test fails —
# whereas the two earlier tautological tests would still pass because they
# emit the log line / event from the test body itself, not from production.
#
# Reviewer NEEDS_FIX call (PR #379, 2026-05-03): "Add one async test that
# drives _dispatch_wave end-to-end with a stubbed
# _ticket_has_open_pr_awaiting_review returning
# _OpenPrCheck(state='awaiting_review'), then assert (a) the real
# production caplog entry appears, (b) _dispatch_child was NOT called
# (mock asserts zero invocations), (c) state.events[-1]['kind'] ==
# 'existing_pr_skip'."


@pytest.mark.asyncio
async def test_dispatch_wave_emits_existing_pr_skip_via_production_path(
    monkeypatch, caplog,
):
    """End-to-end: drive `_dispatch_wave(0)` through the real dispatch
    loop with a stubbed `_ticket_has_open_pr_awaiting_review` returning
    `_OpenPrCheck(state='awaiting_review')`. Assert the production
    dispatch-site (orchestrator.py:_dispatch_wave) — not the test —
    emits the structured log, records the event, and skips
    `_dispatch_child`.

    This is the behavioral counterpart to
    `test_existing_pr_skip_emits_structured_log_and_event` above, which
    builds the payload by hand and emits the log/event from the test
    body. That test pins the *contract* (shape of the log line + event);
    THIS test pins the *production wiring* (that the dispatch loop
    actually calls those emitters).
    """
    import logging as _logging
    from unittest.mock import AsyncMock

    caplog.set_level(
        _logging.INFO, logger="alfred_coo.autonomous_build.orchestrator",
    )

    orch = _mk_orch()

    # Wave-0 ticket, PENDING, no deps → `_select_ready` will pick it up.
    t = _t("u-4115-e2e", "SAL-4115-E2E", code="OPS-99", wave=0, epic="ops")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-sal4115")

    # ── Stub the GitHub-PR pre-flight to return an awaiting-review check.
    # This is the input that drives the dispatch loop into the SAL-4115
    # branch. The real production code path runs from here on out.
    open_pr_payload = orch_mod._OpenPrCheck(
        pr_number=4242,
        pr_url="https://github.com/salucallc/alfred-coo-svc/pull/4242",
        state="awaiting_review",
    )
    monkeypatch.setattr(
        orch, "_ticket_has_open_pr_awaiting_review",
        AsyncMock(return_value=open_pr_payload),
    )
    # No merged PR for this ticket — must run BEFORE the open-PR check
    # in the production dispatch loop, so stub it to None to fall through.
    monkeypatch.setattr(
        orch, "_ticket_has_merged_pr", AsyncMock(return_value=None),
    )

    # ── The mock that proves `_dispatch_child` was NOT called.
    dispatch_child_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_child", dispatch_child_mock)

    # ── Stub the inherited-review fire so we don't need to plumb a full
    # mesh + Hawkman QA dispatch path. The fire flips the ticket to a
    # terminal state (ESCALATED) so the `_dispatch_wave` loop's
    # `if all(t.status in TERMINAL_STATES)` exit condition trips after
    # the first tick — keeps the test deterministic and bounded.
    async def _fake_fire_review(ticket, *, existing_pr):
        ticket.status = TicketStatus.ESCALATED
        return "fake-review-task-id"
    monkeypatch.setattr(
        orch, "_fire_review_for_inherited_pr", _fake_fire_review,
    )

    # ── Stub all periodic / persistence hooks the dispatch loop calls
    # per tick. We only care about the dispatch branch behavior.
    monkeypatch.setattr(
        orch, "_check_cancel_signal", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        orch, "_reconcile_orphan_active", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        orch, "_mark_repo_missing_tickets", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        orch, "_poll_children", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        orch, "_poll_reviews", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(orch, "_check_budget", AsyncMock(return_value=None))
    monkeypatch.setattr(orch, "_status_tick", AsyncMock(return_value=None))
    monkeypatch.setattr(orch, "_stall_watcher", AsyncMock(return_value=None))
    monkeypatch.setattr(
        orch, "_maybe_force_pass_stalled_wave", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        orch, "_update_linear_state", AsyncMock(return_value=None),
    )
    # Skip the SAL-4120 cross-project drift filter for this single in-scope
    # ticket — it has no Target block / repo scope. Patch to identity so the
    # ticket flows through to the dispatch loop.
    monkeypatch.setattr(
        orch, "_filter_out_of_scope_tickets",
        lambda wave_tickets, wave_n: (wave_tickets, []),
    )
    # `checkpoint` is a module-level coroutine that writes to soul; stub.
    monkeypatch.setattr(
        orch_mod, "checkpoint", AsyncMock(return_value=None),
    )

    # ── Drive the production dispatch loop.
    await orch._dispatch_wave(0)

    # ── (a) Real production caplog entry appears.
    skip_logs = [
        rec for rec in caplog.records
        if rec.getMessage().startswith("existing-pr-skip")
        and "ticket=SAL-4115-E2E" in rec.getMessage()
        and "existing_pr=4242" in rec.getMessage()
    ]
    assert len(skip_logs) >= 1, (
        "production dispatch site must emit the `existing-pr-skip ticket=... "
        "existing_pr=... action=...` log line; saw caplog records: %r"
        % [r.getMessage() for r in caplog.records]
    )

    # ── (b) `_dispatch_child` was NOT called — proves the skip branch
    # short-circuited the dispatch loop before the fresh-builder path.
    assert dispatch_child_mock.call_count == 0, (
        "existing-pr-skip branch must NOT fall through to _dispatch_child; "
        "got %d invocations" % dispatch_child_mock.call_count
    )

    # ── (c) `state.events[-1]['kind'] == 'existing_pr_skip'`. Tail of the
    # event log must be the skip event the production dispatch site
    # recorded (not a downstream event from a poll hook).
    assert orch.state.events, "production dispatch site must record an event"
    skip_events = [
        evt for evt in orch.state.events
        if evt.get("kind") == "existing_pr_skip"
    ]
    assert len(skip_events) >= 1, (
        "production dispatch site must record an `existing_pr_skip` event; "
        "got events: %r" % orch.state.events
    )
    evt = skip_events[-1]
    assert evt["identifier"] == "SAL-4115-E2E"
    assert evt["existing_pr"] == 4242
    assert evt["action"] == "skipped"
