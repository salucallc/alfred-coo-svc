"""SAL-4120 — orchestrator wave-cohort assembly skips out-of-scope tickets.

The orchestrator's runtime guard
``AutonomousBuildOrchestrator._filter_out_of_scope_tickets`` parses each
ticket's ``## Target`` block, looks up the orchestrator's
``linear_project_id`` in :data:`ORCHESTRATOR_REPO_SCOPE`, and SKIPs
tickets whose ``owner/repo`` falls outside the configured scope BEFORE
deadlock-grace counting. Without the guard, the wave-2 (07:30Z) and
wave-3 (08:31Z) production deadlocks on 2026-05-03 repeat every 15 min.

These tests assert:

1. A 3-ticket cohort with one out-of-scope ticket returns a 2-ticket
   in-scope list and a 1-tuple skipped list.
2. A structured ``target-repo-skip`` log line is emitted for the skipped
   ticket (with target_repo + orchestrator_scope fields).
3. The skipped ticket's status is NEVER mutated by the filter — the
   downstream deadlock-grace coercion (``ticket_forced_failed_deadlock``)
   never sees it because it's not returned.
4. Tickets without a ``## Target`` block fall through to the legacy
   resolution path (in-scope by default).
5. When the orchestrator's project_id is not in
   :data:`ORCHESTRATOR_REPO_SCOPE`, the filter is a no-op (legacy
   behaviour preserved).
"""

from __future__ import annotations

import logging
from typing import Optional

import pytest

from alfred_coo.autonomous_build.graph import Ticket, TicketGraph, TicketStatus
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    ORCHESTRATOR_REPO_SCOPE,
)


# ── Fakes (parity with test_autonomous_build_orchestrator.py) ──────────────


class _FakeMesh:
    async def create_task(self, **_kw):
        return {"id": "child-x", "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, *_a, **_kw):
        pass


class _FakeSoul:
    async def write_memory(self, content, topics=None):
        return {"memory_id": "m-x"}

    async def recent_memories(self, limit=5, topics=None):
        return []


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


def _mk_persona():
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"
    return P()


def _mk_orchestrator(project_id: str) -> AutonomousBuildOrchestrator:
    """Build an orchestrator pinned to ``project_id`` (skips full
    ``_parse_payload`` flow — sets the field directly so tests don't need
    a kickoff round-trip)."""
    task = {"id": "kick-test", "title": "[persona:autonomous-build-a] kickoff",
            "description": ""}
    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=_FakeMesh(),
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    orch.linear_project_id = project_id
    return orch


def _ticket_with_body(uuid: str, ident: str, body: str, *, wave: int = 1) -> Ticket:
    return Ticket(
        id=uuid,
        identifier=ident,
        code="",
        title=f"{ident} test",
        wave=wave,
        epic="other",
        size="M",
        estimate=5,
        is_critical_path=False,
        body=body,
    )


def _target_block(owner: str, repo: str) -> str:
    return (
        "Some preamble.\n\n"
        "## Target\n"
        f"owner: {owner}\n"
        f"repo:  {repo}\n"
        "paths:\n"
        "  - src/example.py\n"
        "base_branch: main\n"
        "\n"
        "## APE/V Acceptance\n"
        "trailing prose...\n"
    )


# Pick a project_id we know is registered in ORCHESTRATOR_REPO_SCOPE so the
# filter actually engages. MC v1 GA is the canonical anchor (matches the
# 2026-05-03 production evidence).
MC_V1_GA_PROJECT_ID = "8c1d8f69-359d-457a-a11c-2e650863774c"


def test_mc_v1_ga_scope_registered() -> None:
    """Sanity: the canonical MC v1 GA project_id is registered. If this
    test fails, ORCHESTRATOR_REPO_SCOPE has drifted and the runtime guard
    will be a no-op for the project that motivated SAL-4120."""
    assert MC_V1_GA_PROJECT_ID in ORCHESTRATOR_REPO_SCOPE
    assert "salucallc/alfred-coo-svc" in ORCHESTRATOR_REPO_SCOPE[MC_V1_GA_PROJECT_ID]


def test_filter_skips_out_of_scope_and_keeps_in_scope(caplog) -> None:
    """Mixed cohort: 2 in-scope (alfred-coo-svc) + 1 out-of-scope
    (alfred-portal). Filter returns the 2 in-scope tickets and reports the
    out-of-scope one as skipped, with a structured log line."""
    orch = _mk_orchestrator(MC_V1_GA_PROJECT_ID)
    in_a = _ticket_with_body(
        "u-in-a", "SAL-9001",
        _target_block("salucallc", "alfred-coo-svc"),
    )
    in_b = _ticket_with_body(
        "u-in-b", "SAL-9002",
        _target_block("salucallc", "alfred-coo-svc"),
    )
    out_c = _ticket_with_body(
        "u-out-c", "SAL-9003",
        _target_block("salucallc", "alfred-portal"),
    )
    cohort = [in_a, in_b, out_c]

    # Pre-status snapshot — the filter MUST NOT mutate ticket.status.
    pre_status = {t.id: t.status for t in cohort}

    with caplog.at_level(logging.WARNING, logger="alfred_coo.autonomous_build.orchestrator"):
        in_scope, skipped = orch._filter_out_of_scope_tickets(cohort, wave_n=2)

    assert {t.identifier for t in in_scope} == {"SAL-9001", "SAL-9002"}
    assert len(skipped) == 1
    assert skipped[0][0].identifier == "SAL-9003"
    assert skipped[0][1] == "salucallc/alfred-portal"

    # Status untouched — no FAIL coercion attempted.
    for t in cohort:
        assert t.status == pre_status[t.id]

    # Structured log line emitted with the canonical fields.
    skip_lines = [
        rec.getMessage() for rec in caplog.records
        if "target-repo-skip" in rec.getMessage()
    ]
    assert len(skip_lines) == 1, skip_lines
    msg = skip_lines[0]
    assert "ticket=SAL-9003" in msg
    assert "target_repo=salucallc/alfred-portal" in msg
    assert "orchestrator_scope=" in msg
    assert "salucallc/alfred-coo-svc" in msg
    assert "wave=2" in msg

    # State event recorded so doctor signals + audit replay can find it.
    skip_events = [
        ev for ev in orch.state.events
        if ev.get("kind") == "ticket_target_repo_skip"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["identifier"] == "SAL-9003"
    assert skip_events[0]["target_repo"] == "salucallc/alfred-portal"


def test_filter_keeps_tickets_without_target_block() -> None:
    """A ticket whose body has no ``## Target`` block falls through to the
    legacy ``_TARGET_HINTS`` registry path — the filter does NOT skip it."""
    orch = _mk_orchestrator(MC_V1_GA_PROJECT_ID)
    no_target = _ticket_with_body(
        "u-nt", "SAL-9100",
        body="Just some prose, no target block here.",
    )

    in_scope, skipped = orch._filter_out_of_scope_tickets([no_target], wave_n=1)

    assert in_scope == [no_target]
    assert skipped == []


def test_filter_noop_when_project_unregistered() -> None:
    """If the orchestrator's project_id is not in ORCHESTRATOR_REPO_SCOPE
    (e.g. a one-off umbrella project that hasn't been registered yet),
    the filter is a no-op — every ticket passes through unchanged so the
    legacy dispatch path is preserved."""
    orch = _mk_orchestrator("00000000-0000-0000-0000-deadbeefcafe")
    out_of_scope_body = _target_block("salucallc", "alfred-portal")
    t = _ticket_with_body("u-x", "SAL-9200", out_of_scope_body)

    in_scope, skipped = orch._filter_out_of_scope_tickets([t], wave_n=0)

    assert in_scope == [t]
    assert skipped == []


def test_filter_keeps_partial_target_block() -> None:
    """Target block present but missing repo (operator typo) — fall through
    to legacy resolution rather than skipping. The filter is conservative:
    only concrete owner+repo strings can produce a skip."""
    orch = _mk_orchestrator(MC_V1_GA_PROJECT_ID)
    partial_body = (
        "## Target\n"
        "owner: salucallc\n"
        "paths:\n"
        "  - src/x.py\n"
    )
    t = _ticket_with_body("u-p", "SAL-9300", partial_body)

    in_scope, skipped = orch._filter_out_of_scope_tickets([t], wave_n=0)

    assert in_scope == [t]
    assert skipped == []


# ── SAL-4121: scope widening for cross-repo project work ───────────────────
#
# 2026-05-03 drift-sweep audit found 75 records that were NOT ticket-routing
# bugs but symptoms of an over-narrow ORCHESTRATOR_REPO_SCOPE map. Operator
# (Cristian) approved widening per
# Z:/_planning/drift-sweep-triage-2026-05-03.md. These tests pin the new
# (project, repo) pairs so accidental rollback is caught at CI time.

AGENT_INGEST_PROJECT_ID = "9db00c4f-17a4-4b7a-8cd8-ea62f45d55b8"
MSSP_FEDERATION_PROJECT_ID = "a9d93b23-96b4-4a77-be18-b709f72fa3ce"
COCKPIT_CONSUMER_UX_PROJECT_ID = "5a014234-df36-47a0-9abb-eac093e27539"
MSSP_EXTRACTION_PROJECT_ID = "39e340a8-26d2-4439-8582-caf94a263c7e"


@pytest.mark.parametrize(
    "project_id,repo",
    [
        (AGENT_INGEST_PROJECT_ID, "salucallc/saluca-plugins"),
        (AGENT_INGEST_PROJECT_ID, "salucallc/alfred-portal"),
        (MSSP_FEDERATION_PROJECT_ID, "salucallc/soul-svc"),
        (COCKPIT_CONSUMER_UX_PROJECT_ID, "salucallc/alfred-portal"),
        ("8c1d8f69-359d-457a-a11c-2e650863774c", "salucallc/tiresias"),
    ],
)
def test_sal_4121_widened_scope_pairs_in_scope(project_id: str, repo: str) -> None:
    """Each newly-added (project, repo) pair must be in scope after SAL-4121.
    Pin the wired-in mapping at CI time so a future refactor doesn't silently
    revert the operator-approved widening."""
    scope = ORCHESTRATOR_REPO_SCOPE.get(project_id)
    assert scope is not None, f"project_id {project_id} unregistered"
    assert repo in scope, (
        f"{repo} expected in scope for project {project_id}; "
        f"got {sorted(scope)}"
    )


def test_sal_4121_widened_scope_keeps_alfred_coo_svc_anchor() -> None:
    """All registered projects MUST keep ``salucallc/alfred-coo-svc`` as an
    anchor — widening adds repos, never removes the substrate-tier baseline.
    """
    for project_id, scope in ORCHESTRATOR_REPO_SCOPE.items():
        assert "salucallc/alfred-coo-svc" in scope, (
            f"project {project_id} dropped alfred-coo-svc anchor: {sorted(scope)}"
        )


def test_sal_4121_mssp_extraction_remains_single_repo() -> None:
    """MSSP Extraction is intentionally single-repo per the 2026-05-03 audit
    (Cristian explicit: 'don't over-extend'). Pin so accidental drift is
    caught."""
    scope = ORCHESTRATOR_REPO_SCOPE.get(MSSP_EXTRACTION_PROJECT_ID)
    assert scope == frozenset({"salucallc/alfred-coo-svc"}), (
        f"MSSP Extraction scope drifted: {sorted(scope) if scope else None}"
    )


def test_sal_4121_unregistered_repo_not_in_any_scope() -> None:
    """Negative test: a repo that was NOT part of the widening (e.g. a random
    other Saluca repo) must NOT appear in any project's scope."""
    bogus_repo = "salucallc/random-other-repo"
    for project_id, scope in ORCHESTRATOR_REPO_SCOPE.items():
        assert bogus_repo not in scope, (
            f"{bogus_repo} unexpectedly in scope for {project_id}: {sorted(scope)}"
        )
