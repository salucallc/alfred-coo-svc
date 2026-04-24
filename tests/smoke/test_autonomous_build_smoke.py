r"""AB-07 smoke test: 3-ticket end-to-end autonomous_build run.

Drives `AutonomousBuildOrchestrator.run()` through a mocked Linear
project (2 wave-0 tickets + 1 wave-1 ticket that blocks_in on a
wave-0 ticket) with AUTONOMOUS_BUILD_DRY_RUN=1 set. Asserts:

  - orchestrator dispatches both wave-0 tickets
  - wave-1 ticket only dispatches AFTER wave-0 is merged_green
  - all 3 tickets end in merged_green
  - budget tracker cumulative_spend_usd > 0 (tokens counted)
  - Slack cadence posted at least 1 message
  - no real HTTP issued (DryRunMesh is the only mesh surface)
  - full run completes in <10 seconds wall-clock

The smoke test is gated by the `smoke` pytest marker so regular
`pytest` runs skip it. Run it explicitly via:

    AUTONOMOUS_BUILD_DRY_RUN=1 pytest tests/smoke/test_autonomous_build_smoke.py -v -m smoke

On Windows PowerShell:

    $env:AUTONOMOUS_BUILD_DRY_RUN=1; pytest tests\smoke\test_autonomous_build_smoke.py -v -m smoke

The env var must be set BEFORE the orchestrator is instantiated — the
`autouse` fixture below handles it.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from alfred_coo.autonomous_build.dry_run import DryRunMesh
from alfred_coo.autonomous_build.graph import TicketStatus
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)


pytestmark = pytest.mark.smoke


# -- Fakes for soul + settings (mesh is replaced by DryRunMesh) -------------


class _FakeSoul:
    """Minimal soul client for checkpoint/restore. Writes accumulate;
    recent_memories returns the newest write first so the orchestrator's
    restore path works (though we run from scratch, not restart)."""

    def __init__(self):
        self.writes = []

    async def write_memory(self, content, topics=None):
        self.writes.append({"content": content, "topics": topics or []})
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        # Fresh-start path; return empty so orchestrator starts cleanly.
        return []


class _FakeSettings:
    soul_session_id = "smoke-session"
    soul_node_id = "smoke-node"
    soul_harness = "pytest-smoke"


def _mk_persona():
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"
    return P()


# -- Autouse env-var fixture ------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_dry_run(monkeypatch):
    """Ensure AUTONOMOUS_BUILD_DRY_RUN is set for every test in this module.

    Orchestrator reads the env var in __init__, so the fixture must run
    BEFORE the orchestrator is instantiated inside the test body.
    """
    monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", "1")


# -- 3-ticket mock project fixture ------------------------------------------


def _three_ticket_project() -> list[dict]:
    """Return the canned issues list the smoke test injects as the
    Linear project payload.

    - SAL-501 (wave-0, epic=ops, critical-path)
    - SAL-502 (wave-0, epic=ops)
    - SAL-503 (wave-1, epic=ops, blocked_by=SAL-501)
    """
    return [
        {
            "id": "uuid-501",
            "identifier": "SAL-501",
            "title": "OPS-10 first wave-0 ticket",
            "labels": ["wave-0", "ops", "critical-path"],
            "estimate": 1,
            "state": {"name": "Backlog"},
            "relations": [],
        },
        {
            "id": "uuid-502",
            "identifier": "SAL-502",
            "title": "OPS-11 second wave-0 ticket",
            "labels": ["wave-0", "ops"],
            "estimate": 1,
            "state": {"name": "Backlog"},
            "relations": [],
        },
        {
            "id": "uuid-503",
            "identifier": "SAL-503",
            "title": "OPS-12 wave-1 ticket blocked on 501",
            "labels": ["wave-1", "ops"],
            "estimate": 1,
            "state": {"name": "Backlog"},
            "relations": [
                {
                    "type": "blocked_by",
                    "relatedIssue": {
                        "id": "uuid-501",
                        "identifier": "SAL-501",
                    },
                },
            ],
        },
    ]


def _build_orchestrator() -> AutonomousBuildOrchestrator:
    kickoff_payload = {
        "linear_project_id": "proj-smoke",
        "concurrency": {"max_parallel_subs": 6, "per_epic_cap": 3},
        "budget": {"max_usd": 30.0},
        "wave_order": [0, 1],
        "on_all_green": [],
        "status_cadence": {
            "interval_minutes": 1,
            "slack_channel": "C-SMOKE",
        },
    }
    task = {
        "id": "kick-smoke",
        "title": "[persona:autonomous-build-a] smoke kickoff",
        "description": json.dumps(kickoff_payload),
    }
    # Initial mesh + soul — mesh will be swapped by maybe_apply_dry_run
    # inside __init__, but soul stays real (FakeSoul for tests).
    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=object(),  # replaced immediately by DryRunMesh
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )

    # Shorten loops for speed.
    orch.poll_sleep_sec = 0

    # The DryRunAdapter defaults to 1s auto-complete; 0.05s is plenty
    # for a CI smoke that wants to stay under 10s.
    assert orch._dry_run_adapter is not None, "dry-run must be active"
    orch._dry_run_adapter.auto_complete_after_seconds = 0.05

    # Inject canned Linear fetchers.
    async def fake_list(project_id, limit=250):
        return {
            "issues": _three_ticket_project(),
            "total": 3,
            "truncated": False,
        }

    async def fake_rel(issue_id):
        return {"blocks": [], "blocked_by": [], "related": []}

    orch._list_project_issues = fake_list
    orch._get_issue_relations = fake_rel

    return orch


# -- The big one ------------------------------------------------------------


async def test_smoke_3_ticket_end_to_end_happy_path(capsys):
    """Drive the orchestrator end-to-end through a 3-ticket mock project.

    Verifies wave sequencing, dep gating, budget accounting, slack
    cadence emission, and that the whole run stays under the 10s
    wall-clock budget.
    """
    orch = _build_orchestrator()
    adapter = orch._dry_run_adapter
    assert isinstance(orch.mesh, DryRunMesh), (
        "orchestrator.mesh must be the DryRunMesh shim, not a real client"
    )

    started = time.monotonic()
    await asyncio.wait_for(orch.run(), timeout=10.0)
    elapsed = time.monotonic() - started
    print(f"[SMOKE] orchestrator completed in {elapsed:.2f}s")

    # -- no real HTTP -----------------------------------------------------
    # The DryRunMesh is in place AND nothing in the orchestrator's wiring
    # grabbed a real MeshClient. We assert the synthesized task ids are
    # all `dryrun-*` (the adapter-only id prefix).
    for entry in adapter._tasks.values():
        assert entry["record"]["id"].startswith("dryrun-"), (
            f"non-dryrun task leaked into adapter: {entry['record']!r}"
        )

    # -- 3 ticket children dispatched + kickoff completion ---------------
    # adapter._tasks includes the 3 child dispatches PLUS the hawkman-qa-a
    # review tasks that the orchestrator auto-fires when each child opens
    # a PR (added in AB-08). Filter to the alfred-coo-a (builder) persona
    # for the wave-ordering assertions; count reviews separately for the
    # sanity checks below.
    # The kickoff mesh task is a DIFFERENT id (kick-smoke), completed via
    # mesh.complete; it lands in adapter.completions.
    child_titles = [e["record"]["title"] for e in adapter._tasks.values()]
    builder_titles = [t for t in child_titles if "[persona:alfred-coo-a]" in t]
    review_titles = [t for t in child_titles if "[persona:hawkman-qa-a]" in t]
    assert len(builder_titles) >= 3, builder_titles
    # One review fires per PR opened, so we expect ≥3 review tasks too in
    # the happy-path dry-run (APPROVE on first review).
    assert len(review_titles) >= 3, review_titles

    # -- wave ordering ---------------------------------------------------
    # First two builder dispatches are wave-0; the wave-1 ticket only
    # dispatches after SAL-501 reaches merged_green.
    tasks_in_order = sorted(
        adapter._tasks.items(),
        key=lambda kv: kv[1]["created_at"],
    )
    titles_in_order = [e[1]["record"]["title"] for e in tasks_in_order]
    builder_titles_in_order = [
        t for t in titles_in_order if "[persona:alfred-coo-a]" in t
    ]
    wave_0_titles = [t for t in builder_titles_in_order if "[wave-0]" in t]
    wave_1_titles = [t for t in builder_titles_in_order if "[wave-1]" in t]
    assert len(wave_0_titles) == 2, builder_titles_in_order
    assert len(wave_1_titles) == 1, builder_titles_in_order

    # The wave-1 dispatch must appear AFTER both wave-0 dispatches.
    wave_1_index = builder_titles_in_order.index(wave_1_titles[0])
    wave_0_max_index = max(
        builder_titles_in_order.index(t) for t in wave_0_titles
    )
    assert wave_1_index > wave_0_max_index, (
        f"wave-1 dispatched before wave-0 finished: {builder_titles_in_order}"
    )

    # -- all 3 tickets merged_green -------------------------------------
    statuses = {t.identifier: t.status for t in orch.graph.nodes.values()}
    assert statuses == {
        "SAL-501": TicketStatus.MERGED_GREEN,
        "SAL-502": TicketStatus.MERGED_GREEN,
        "SAL-503": TicketStatus.MERGED_GREEN,
    }, statuses

    # -- budget tracker ticked --------------------------------------------
    spend = orch.budget_tracker.cumulative_spend
    # Each child returns tokens.in=100, tokens.out=50 on qwen3-coder:480b
    # ($0.30 input / $1.20 output per Mtok) -> ~$0.00009/task.
    assert spend > 0.0, f"cumulative_spend_usd must be > 0, got {spend!r}"
    # And state.cumulative_spend_usd should mirror the tracker.
    assert orch.state.cumulative_spend_usd == pytest.approx(spend)

    # -- slack cadence posted at least once ------------------------------
    assert len(adapter.slack_posts) >= 1, (
        "Expected >=1 slack cadence post; saw 0"
    )
    # Stdout prefix is emitted for every adapter.slack_post call.
    captured = capsys.readouterr()
    assert "[DRY-RUN slack]" in captured.out

    # -- kickoff marked completed ---------------------------------------
    assert len(adapter.completions) == 1
    comp = adapter.completions[0]
    assert comp["task_id"] == "kick-smoke"
    assert "merged_green" in comp["result"]["summary"]

    # -- state was checkpointed ------------------------------------------
    assert len(orch.soul.writes) >= 1

    # -- wall-clock budget -----------------------------------------------
    assert elapsed < 10.0, f"smoke run exceeded 10s budget: {elapsed:.2f}s"


async def test_smoke_respects_dry_run_env_flag(monkeypatch):
    """Sanity check — without the env var, the orchestrator should NOT
    auto-wire the DryRunMesh. This is the negative companion to the big
    test above."""
    monkeypatch.delenv("AUTONOMOUS_BUILD_DRY_RUN", raising=False)

    class _NotAMesh:
        sentinel = True

    task = {
        "id": "kick-nodryrun",
        "title": "[persona:autonomous-build-a] no-dryrun",
        "description": "{}",
    }
    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=_NotAMesh(),
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    assert orch._dry_run_adapter is None
    assert getattr(orch.mesh, "sentinel", False) is True
