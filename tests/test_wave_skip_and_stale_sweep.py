"""Tests for the wave-skip + stale "In Progress" sweeper optimizations.

Covers Fix A (wave-skip cache) + Fix B (stale-sweep) added in the
``feat/wave-skip-and-stale-state-sweeper`` branch.

Fix A: ``_should_skip_wave`` reads the persisted wave-pass record (keyed
by ``(linear_project_id, wave_n)``) and returns True iff a recent pass
was at ratio=1.00 AND the wave's ticket set has not regressed.

Fix B: ``_sweep_stale_in_progress`` walks Linear "In Progress" tickets
and flips them to Done when a recent merged PR cites the ticket
identifier.
"""

from __future__ import annotations

import json
import time

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import AutonomousBuildOrchestrator
from alfred_coo.autonomous_build.state import (
    WavePassRecord,
    record_wave_pass,
    lookup_wave_pass,
    wave_pass_topic_for,
)


# ── Fakes (mirror tests/test_autonomous_build_orchestrator.py) ─────────────


class _FakeMesh:
    def __init__(self):
        self.created: list[dict] = []
        self.completions: list[dict] = []

    async def create_task(self, *, title, description="", from_session_id=None):
        self.created.append({"title": title, "description": description})
        return {"id": "child-1", "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        self.completions.append({"task_id": task_id, "status": status})


class _FakeSoul:
    """Recording soul double — same shape as the orchestrator test fakes.

    ``recent_memories`` returns reverse-chronological matches by topic
    (matches soul-svc /v1/memory/recent semantics).
    """

    def __init__(self, initial: list[dict] | None = None):
        self.writes: list[dict] = []
        self.reads: list[dict] = list(initial or [])

    async def write_memory(self, content, topics=None):
        rec = {"content": content, "topics": topics or []}
        self.writes.append(rec)
        self.reads.insert(0, rec)
        return {"memory_id": f"m-{len(self.writes)}"}

    async def recent_memories(self, limit=5, topics=None):
        if topics:
            filtered = [
                m for m in self.reads
                if any(t in (m.get("topics") or []) for t in topics)
            ]
        else:
            filtered = list(self.reads)
        return filtered[:limit]


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


def _mk_persona():
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"
    return P()


def _mk_orch(*, kickoff: dict | None = None, soul=None, mesh=None) -> AutonomousBuildOrchestrator:
    kickoff = kickoff or {"linear_project_id": "PROJ-AAAA"}
    task = {
        "id": "kick-test",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": json.dumps(kickoff),
    }
    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    # Mirror what _parse_payload would do without forcing a graph build.
    orch.linear_project_id = kickoff["linear_project_id"]
    return orch


def _t(uuid: str, ident: str, code: str, wave: int, *, status: TicketStatus = TicketStatus.PENDING) -> Ticket:
    t = Ticket(
        id=uuid, identifier=ident, code=code, title=f"{ident} {code}",
        wave=wave, epic="tiresias", size="M", estimate=5,
        is_critical_path=False,
    )
    t.status = status
    return t


def _seed(orch: AutonomousBuildOrchestrator, tickets: list[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


# ── Fix A · Wave-skip cache ────────────────────────────────────────────────


async def test_record_and_lookup_wave_pass_round_trip():
    """Write a 1.00 pass record, read it back via lookup_wave_pass."""
    soul = _FakeSoul()
    proj = "PROJ-AAAA"
    resp = await record_wave_pass(
        soul,
        linear_project_id=proj,
        wave_n=0,
        ratio=1.0,
        denominator=5,
        green_count=5,
        ticket_codes_seen=["SAL-1", "SAL-2", "SAL-3", "SAL-4", "SAL-5"],
    )
    assert resp is not None
    # Topic includes the canonical wave-pass key.
    assert any(
        wave_pass_topic_for(proj, 0) in (w["topics"] or [])
        for w in soul.writes
    )

    record = await lookup_wave_pass(soul, linear_project_id=proj, wave_n=0)
    assert record is not None
    assert record.linear_project_id == proj
    assert record.wave_n == 0
    assert record.ratio == 1.0
    assert record.denominator == 5
    assert record.green_count == 5
    # Sorted on write so order is deterministic for diffing.
    assert record.ticket_codes_seen == [
        "SAL-1", "SAL-2", "SAL-3", "SAL-4", "SAL-5",
    ]


async def test_lookup_wave_pass_returns_none_when_no_record():
    soul = _FakeSoul()
    record = await lookup_wave_pass(soul, linear_project_id="X", wave_n=0)
    assert record is None


async def test_should_skip_wave_skips_when_prior_pass_at_1_00():
    """Wave 0 with persisted 1.00 pass + matching graph -> skipped."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    # Pre-seed the cache with a fresh 1.00 pass.
    await record_wave_pass(
        soul,
        linear_project_id=proj,
        wave_n=0,
        ratio=1.0,
        denominator=2,
        green_count=2,
        ticket_codes_seen=["SAL-1", "SAL-2"],
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    # Live graph mirrors the cached set, all MERGED_GREEN.
    _seed(orch, [
        _t("u1", "SAL-1", "TIR-01", 0, status=TicketStatus.MERGED_GREEN),
        _t("u2", "SAL-2", "TIR-02", 0, status=TicketStatus.MERGED_GREEN),
    ])
    assert await orch._should_skip_wave(0) is True


async def test_should_skip_wave_re_evaluates_when_prior_pass_below_1_00():
    """A 0.9 (soft-green) pass should NOT skip on re-entry — soft-greens
    are deliberately not cached; defend the consumer side too."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    # Manually inject a sub-1.00 record (record_wave_pass would store it,
    # but we want to assert the consumer rejects it regardless).
    blob = WavePassRecord(
        linear_project_id=proj,
        wave_n=0,
        ratio=0.9,
        passed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        denominator=10,
        green_count=9,
        ticket_codes_seen=["SAL-1"],
    ).to_json()
    await soul.write_memory(blob, topics=[wave_pass_topic_for(proj, 0)])

    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    _seed(orch, [_t("u1", "SAL-1", "TIR-01", 0, status=TicketStatus.MERGED_GREEN)])
    assert await orch._should_skip_wave(0) is False


async def test_should_skip_wave_re_evaluates_when_ticket_regressed():
    """Cached 1.00 pass + ticket that regressed (Done -> Backlog
    surfaces as status != MERGED_GREEN locally) -> re-evaluate."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    await record_wave_pass(
        soul,
        linear_project_id=proj,
        wave_n=0,
        ratio=1.0,
        denominator=2,
        green_count=2,
        ticket_codes_seen=["SAL-1", "SAL-2"],
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    # SAL-2 regressed from MERGED_GREEN to PENDING (e.g. moved Done -> Backlog
    # in Linear and ``_apply_restored_status`` mirrored it locally).
    _seed(orch, [
        _t("u1", "SAL-1", "TIR-01", 0, status=TicketStatus.MERGED_GREEN),
        _t("u2", "SAL-2", "TIR-02", 0, status=TicketStatus.PENDING),
    ])
    assert await orch._should_skip_wave(0) is False


async def test_should_skip_wave_re_evaluates_when_new_ticket_added():
    """Cached pass over {SAL-1,SAL-2}, current graph has SAL-3 too -> skip
    must be False so the new ticket gets dispatched."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    await record_wave_pass(
        soul,
        linear_project_id=proj,
        wave_n=0,
        ratio=1.0,
        denominator=2,
        green_count=2,
        ticket_codes_seen=["SAL-1", "SAL-2"],
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    _seed(orch, [
        _t("u1", "SAL-1", "TIR-01", 0, status=TicketStatus.MERGED_GREEN),
        _t("u2", "SAL-2", "TIR-02", 0, status=TicketStatus.MERGED_GREEN),
        # Newly added since cache was written:
        _t("u3", "SAL-3", "TIR-03", 0, status=TicketStatus.PENDING),
    ])
    assert await orch._should_skip_wave(0) is False


async def test_should_skip_wave_re_evaluates_when_pass_is_stale():
    """A 1.00 pass older than the freshness window must NOT skip."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    # Hand-craft a stale record (~48h ago) so we don't depend on time.time
    # mocks for the test path. record.passed_at format matches
    # state._calendar_timegm round-trip.
    stale_struct = time.gmtime(time.time() - 48 * 3600)
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", stale_struct)
    blob = WavePassRecord(
        linear_project_id=proj,
        wave_n=0,
        ratio=1.0,
        passed_at=stale_iso,
        denominator=2,
        green_count=2,
        ticket_codes_seen=["SAL-1", "SAL-2"],
    ).to_json()
    await soul.write_memory(blob, topics=[wave_pass_topic_for(proj, 0)])

    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    _seed(orch, [
        _t("u1", "SAL-1", "TIR-01", 0, status=TicketStatus.MERGED_GREEN),
        _t("u2", "SAL-2", "TIR-02", 0, status=TicketStatus.MERGED_GREEN),
    ])
    assert await orch._should_skip_wave(0) is False


async def test_should_skip_wave_returns_false_without_project_id():
    """Defensive: empty project id should never short-circuit a wave."""
    soul = _FakeSoul()
    orch = _mk_orch(kickoff={"linear_project_id": "PROJ"}, soul=soul)
    orch.linear_project_id = ""
    _seed(orch, [_t("u1", "SAL-1", "TIR-01", 0)])
    assert await orch._should_skip_wave(0) is False


# ── Fix B · Stale "In Progress" sweeper ────────────────────────────────────


def _install_fake_linear_tools(monkeypatch, *, list_response, update_recorder,
                                comment_recorder=None):
    """Install fake BUILTIN_TOOLS entries for the stale-sweep helpers.

    The orchestrator imports ``alfred_coo.tools`` lazily inside
    ``_sweep_stale_in_progress``; we patch the module dict to swap the
    ToolSpec handlers for our recorders.
    """
    from alfred_coo import tools as tools_mod

    class _Spec:
        def __init__(self, handler):
            self.handler = handler

    async def fake_list(project_id, limit=250):
        return list_response

    async def fake_update(*, issue_id, state_name):
        update_recorder.append({"issue_id": issue_id, "state_name": state_name})
        return {"ok": True, "identifier": issue_id, "state": state_name}

    async def fake_comment(*, issue_id, body):
        if comment_recorder is not None:
            comment_recorder.append({"issue_id": issue_id, "body": body})
        return {"ok": True}

    fake_tools = {
        "linear_list_project_issues": _Spec(fake_list),
        "linear_update_issue_state": _Spec(fake_update),
    }
    if comment_recorder is not None:
        fake_tools["linear_add_comment"] = _Spec(fake_comment)

    # The sweep does ``from alfred_coo.tools import BUILTIN_TOOLS``; patch
    # the attribute on the live module so the import sees our fakes.
    monkeypatch.setattr(tools_mod, "BUILTIN_TOOLS", fake_tools, raising=False)


async def test_sweep_stale_in_progress_flips_ticket_with_merged_pr(monkeypatch):
    """Ticket in In Progress + matching merged PR -> flipped to Done.
    Audit comment posted via linear_add_comment when the tool exists."""
    proj = "PROJ-AAAA"
    list_response = {
        "issues": [
            {
                "id": "uuid-2656",
                "identifier": "SAL-2656",
                "title": "OPS-23 model pricing loader",
                "labels": ["wave-1"],
                "estimate": 3,
                "state": {"name": "In Progress"},
                "relations": [],
            },
            # Ticket in another state — must NOT be touched.
            {
                "id": "uuid-other",
                "identifier": "SAL-9999",
                "title": "OPS-99 something",
                "labels": [],
                "estimate": 1,
                "state": {"name": "Backlog"},
                "relations": [],
            },
        ],
    }
    updates: list[dict] = []
    comments: list[dict] = []
    _install_fake_linear_tools(
        monkeypatch,
        list_response=list_response,
        update_recorder=updates,
        comment_recorder=comments,
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # 2026-04-26 tightening: matcher now requires a hint + file overlap.
    # Seed graph so SAL-2656 -> OPS-23 hint resolves.
    g = TicketGraph()
    t = _t("uuid-2656", "SAL-2656", "OPS-23", 1, status=TicketStatus.IN_PROGRESS)
    g.nodes[t.id] = t
    g.identifier_index[t.identifier] = t.id
    orch.graph = g

    async def fake_pr_search(ident: str):
        if ident == "SAL-2656":
            return "https://github.com/salucallc/alfred-coo-svc/pull/145"
        return None
    orch._gh_pr_search_fn = fake_pr_search

    async def fake_pr_files(pr_url: str):
        # PR #145 is a real OPS-23 implementation: configs/model_pricing.yaml
        # is in OPS-23.new_paths, so this satisfies the file-overlap rule.
        return ("configs/model_pricing.yaml", "src/alfred_coo/pricing.py")
    orch._gh_pr_files_fn = fake_pr_files

    flipped = await orch._sweep_stale_in_progress(proj)
    assert flipped == 1
    # SAL-2656 was flipped, SAL-9999 was untouched (already not In Progress).
    assert len(updates) == 1
    assert updates[0]["issue_id"] == "uuid-2656"
    assert updates[0]["state_name"] == "Done"
    # Audit comment posted with PR URL.
    assert len(comments) == 1
    assert comments[0]["issue_id"] == "uuid-2656"
    assert "merged PR found" in comments[0]["body"]
    assert "/pull/145" in comments[0]["body"]
    # State event recorded for downstream auditors.
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_auto_flipped_stale" in kinds


async def test_sweep_stale_in_progress_leaves_ticket_without_pr(monkeypatch):
    """Ticket in In Progress + no merged PR -> untouched (genuine
    in-flight; we must not flip)."""
    proj = "PROJ-AAAA"
    list_response = {
        "issues": [
            {
                "id": "uuid-genuine",
                "identifier": "SAL-7777",
                "title": "TIR-99 in-flight build",
                "labels": ["wave-2"],
                "estimate": 3,
                "state": {"name": "In Progress"},
                "relations": [],
            },
        ],
    }
    updates: list[dict] = []
    _install_fake_linear_tools(
        monkeypatch,
        list_response=list_response,
        update_recorder=updates,
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj})

    async def fake_pr_search(ident: str):
        return None  # no PR ever found
    orch._gh_pr_search_fn = fake_pr_search

    flipped = await orch._sweep_stale_in_progress(proj)
    assert flipped == 0
    assert updates == []
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_auto_flipped_stale" not in kinds


async def test_sweep_stale_in_progress_no_op_on_empty_project(monkeypatch):
    """Empty project id should immediately return 0 with no calls."""
    orch = _mk_orch(kickoff={"linear_project_id": "PROJ"})
    flipped = await orch._sweep_stale_in_progress("")
    assert flipped == 0


async def test_sweep_stale_in_progress_skips_tickets_in_other_states(monkeypatch):
    """Sweeper case-insensitively matches `In Progress`; everything else
    (Done, Backlog, Cancelled, In Review) is left alone even if a merged
    PR exists for it."""
    proj = "PROJ-AAAA"
    list_response = {
        "issues": [
            {
                "id": "uuid-done",
                "identifier": "SAL-100",
                "title": "OPS-100",
                "labels": [],
                "estimate": 1,
                "state": {"name": "Done"},
                "relations": [],
            },
            {
                "id": "uuid-backlog",
                "identifier": "SAL-101",
                "title": "OPS-101",
                "labels": [],
                "estimate": 1,
                "state": {"name": "Backlog"},
                "relations": [],
            },
            {
                "id": "uuid-review",
                "identifier": "SAL-102",
                "title": "OPS-102",
                "labels": [],
                "estimate": 1,
                "state": {"name": "In Review"},
                "relations": [],
            },
        ],
    }
    updates: list[dict] = []
    _install_fake_linear_tools(
        monkeypatch,
        list_response=list_response,
        update_recorder=updates,
    )
    orch = _mk_orch(kickoff={"linear_project_id": proj})

    async def fake_pr_search(ident: str):
        # Pretend every ticket has a PR; sweeper must still skip them
        # because none are In Progress.
        return f"https://github.com/salucallc/alfred-coo-svc/pull/{ident}"
    orch._gh_pr_search_fn = fake_pr_search

    flipped = await orch._sweep_stale_in_progress(proj)
    assert flipped == 0
    assert updates == []


# ── Fix B+ · PR-to-ticket file-overlap matcher (2026-04-26 tightening) ──────
#
# Regression coverage for the 2026-04-26 verification audit, which caught
# the sweeper auto-flipping TIR-15 / OPS-08 / F19 to Done based purely on
# a GitHub-Search code hit, even though the merged PR only edited the
# ``_TARGET_HINTS`` dict in
# ``src/alfred_coo/autonomous_build/orchestrator.py``. The matcher now
# also requires the merged PR's changed files to intersect the ticket's
# expected scope (paths ∪ new_paths) before treating it as evidence.


def _seed_with_code(orch, identifier: str, code: str) -> None:
    """Seed orch.graph with a single ticket whose ``code`` keys into
    ``_TARGET_HINTS``. Used by the file-overlap regression tests so the
    matcher can resolve identifier -> code -> hint."""
    g = TicketGraph()
    t = _t("u-overlap", identifier, code, 0, status=TicketStatus.IN_PROGRESS)
    g.nodes[t.id] = t
    g.identifier_index[t.identifier] = t.id
    orch.graph = g


def test_pr_files_match_hint_pure_helper_truth_table():
    """Pure-function intersection rule: scope hit -> True; no hit / only
    non-evidence file / empty hint / empty diff -> False."""
    from alfred_coo.autonomous_build.orchestrator import (
        AutonomousBuildOrchestrator,
        TargetHint,
    )

    hint_paths = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("plans/v1-ga/OPS-08.md",),
        new_paths=("scripts/migrate_state_secrets.sh", "MIGRATION.md"),
    )

    # Diff inside hint.paths -> match.
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        ("plans/v1-ga/OPS-08.md", "README.md"), hint_paths,
    ) is True
    # Diff inside hint.new_paths -> match.
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        ("scripts/migrate_state_secrets.sh",), hint_paths,
    ) is True
    # Diff entirely outside both -> no match (CATCHES THE BUG).
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        ("docs/UNRELATED.md", "tests/test_other.py"), hint_paths,
    ) is False
    # Diff is *only* the orchestrator hints file -> no match
    # (CATCHES THE BUG; PRs #73/#109/#120/#126 pattern).
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        ("src/alfred_coo/autonomous_build/orchestrator.py",), hint_paths,
    ) is False
    # No hint at all -> no match (cannot verify scope).
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        ("anything.py",), None,
    ) is False
    # Empty diff -> no match.
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        (), hint_paths,
    ) is False


def test_pr_files_match_hint_orchestrator_plus_real_file_still_matches():
    """A PR that touches orchestrator.py *and* a real implementation file
    must still match — we only short-circuit the pure-hints-table case."""
    from alfred_coo.autonomous_build.orchestrator import (
        AutonomousBuildOrchestrator,
        TargetHint,
    )
    hint = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=(),
        new_paths=("src/mcctl/commands/policy.py",),
    )
    # F19 misfire pattern, but PR also includes the real file -> match.
    assert AutonomousBuildOrchestrator._pr_files_match_hint(
        (
            "src/alfred_coo/autonomous_build/orchestrator.py",
            "src/mcctl/commands/policy.py",
        ),
        hint,
    ) is True


async def test_find_recent_merged_pr_rejects_orchestrator_only_pr(monkeypatch):
    """Regression for TIR-15 / OPS-08 / F19 misfire: a search hit on a
    PR whose only changed file is the orchestrator hints table must be
    rejected, NOT returned as evidence the ticket shipped."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # OPS-08 has a real _TARGET_HINTS entry; seed identifier->code so
    # the matcher can look up the hint.
    _seed_with_code(orch, "SAL-2641", "OPS-08")

    async def fake_search(ident: str):
        # Sweeper finds PR #73-style merged PR mentioning OPS-08.
        return "https://github.com/salucallc/alfred-coo-svc/pull/73"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        # PR only touches the orchestrator hints table.
        return ("src/alfred_coo/autonomous_build/orchestrator.py",)
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-2641")
    assert result is None, (
        "PRs that only edit the _TARGET_HINTS orchestrator file must be "
        "rejected; this is the 2026-04-26 regression case (OPS-08 etc.)"
    )


async def test_find_recent_merged_pr_rejects_diff_outside_hint_scope(monkeypatch):
    """A PR that mentions the ticket code but whose diff is entirely
    outside the ticket's expected paths must be rejected."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # TIR-15 has a real hint with deploy/appliance/tiresias/* paths.
    _seed_with_code(orch, "SAL-2597", "TIR-15")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/120"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        # Diff is all in unrelated docs / test fixtures.
        return ("docs/random.md", "tests/test_unrelated.py")
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-2597")
    assert result is None, (
        "PR diff outside hint.paths∪new_paths must not count as evidence"
    )


async def test_find_recent_merged_pr_accepts_diff_inside_paths(monkeypatch):
    """Positive case: PR diff lands inside hint.paths -> match."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # OPS-01 hint.paths = ("deploy/appliance/docker-compose.yml",)
    _seed_with_code(orch, "SAL-OPS01", "OPS-01")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/200"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        # Real implementation file in hint.paths -> should match.
        return ("deploy/appliance/docker-compose.yml", "README.md")
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-OPS01")
    assert result == "https://github.com/salucallc/alfred-coo-svc/pull/200"


async def test_find_recent_merged_pr_accepts_diff_inside_new_paths(monkeypatch):
    """Positive case: PR diff lands inside hint.new_paths -> match."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # OPS-02 hint.new_paths = ("deploy/appliance/IMAGE_PINS.md",)
    _seed_with_code(orch, "SAL-OPS02", "OPS-02")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/201"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        return ("deploy/appliance/IMAGE_PINS.md",)
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-OPS02")
    assert result == "https://github.com/salucallc/alfred-coo-svc/pull/201"


async def test_find_recent_merged_pr_rejects_when_no_hint_for_code(monkeypatch):
    """Defence-in-depth: a ticket whose code has no _TARGET_HINTS entry
    cannot be auto-flipped — we have no scope contract to verify."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    # Use a code that is intentionally not in _TARGET_HINTS.
    _seed_with_code(orch, "SAL-NOHINT", "DOES-NOT-EXIST-99")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/999"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        return ("src/anything.py",)
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-NOHINT")
    assert result is None


async def test_find_recent_merged_pr_walks_candidates_until_a_match(monkeypatch):
    """Multiple search hits: matcher walks them in rank order and returns
    the first one whose diff intersects the hint."""
    proj = "PROJ-AAAA"
    orch = _mk_orch(kickoff={"linear_project_id": proj})
    _seed_with_code(orch, "SAL-OPS01", "OPS-01")

    async def fake_search(ident: str):
        # Newer test stubs may return a list; the matcher accepts both.
        return [
            "https://github.com/salucallc/alfred-coo-svc/pull/300",  # bad
            "https://github.com/salucallc/alfred-coo-svc/pull/301",  # bad
            "https://github.com/salucallc/alfred-coo-svc/pull/302",  # good
        ]
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        if pr_url.endswith("/300"):
            return ("src/alfred_coo/autonomous_build/orchestrator.py",)
        if pr_url.endswith("/301"):
            return ("docs/elsewhere.md",)
        # 302 is the real implementation.
        return ("deploy/appliance/docker-compose.yml",)
    orch._gh_pr_files_fn = fake_files

    result = await orch._find_recent_merged_pr_for("SAL-OPS01")
    assert result == "https://github.com/salucallc/alfred-coo-svc/pull/302"


async def test_sweep_does_not_flip_when_pr_only_touches_orchestrator(monkeypatch):
    """End-to-end: a stale-sweep tick with a search-hit but
    orchestrator-only diff must NOT flip the ticket. This is the exact
    misfire pattern that produced the 2026-04-26 false positives
    (TIR-15 / OPS-08 / F19)."""
    proj = "PROJ-AAAA"
    list_response = {
        "issues": [
            {
                "id": "uuid-2641",
                "identifier": "SAL-2641",
                "title": "OPS-08 migrate state secrets",
                "labels": ["wave-2"],
                "estimate": 3,
                "state": {"name": "In Progress"},
                "relations": [],
            },
        ],
    }
    updates: list[dict] = []
    comments: list[dict] = []
    _install_fake_linear_tools(
        monkeypatch,
        list_response=list_response,
        update_recorder=updates,
        comment_recorder=comments,
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj})
    _seed_with_code(orch, "SAL-2641", "OPS-08")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/73"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        # PR #73 pattern: only edits the orchestrator hints table.
        return ("src/alfred_coo/autonomous_build/orchestrator.py",)
    orch._gh_pr_files_fn = fake_files

    flipped = await orch._sweep_stale_in_progress(proj)
    assert flipped == 0, (
        "Sweep must not auto-flip when the only matching PR is a "
        "_TARGET_HINTS-only edit"
    )
    assert updates == []
    assert comments == []
    kinds = [e["kind"] for e in orch.state.events]
    assert "ticket_auto_flipped_stale" not in kinds


async def test_sweep_still_flips_legitimate_implementation_pr(monkeypatch):
    """End-to-end: when the merged PR's diff actually intersects the
    ticket's expected paths, the sweep behaves exactly as before."""
    proj = "PROJ-AAAA"
    list_response = {
        "issues": [
            {
                "id": "uuid-ops01",
                "identifier": "SAL-OPS01",
                "title": "OPS-01 mc-ops network",
                "labels": ["wave-1"],
                "estimate": 3,
                "state": {"name": "In Progress"},
                "relations": [],
            },
        ],
    }
    updates: list[dict] = []
    comments: list[dict] = []
    _install_fake_linear_tools(
        monkeypatch,
        list_response=list_response,
        update_recorder=updates,
        comment_recorder=comments,
    )

    orch = _mk_orch(kickoff={"linear_project_id": proj})
    _seed_with_code(orch, "SAL-OPS01", "OPS-01")

    async def fake_search(ident: str):
        return "https://github.com/salucallc/alfred-coo-svc/pull/400"
    orch._gh_pr_search_fn = fake_search

    async def fake_files(pr_url: str):
        # Real OPS-01 implementation path.
        return ("deploy/appliance/docker-compose.yml",)
    orch._gh_pr_files_fn = fake_files

    flipped = await orch._sweep_stale_in_progress(proj)
    assert flipped == 1
    assert len(updates) == 1
    assert updates[0]["issue_id"] == "uuid-ops01"
    assert updates[0]["state_name"] == "Done"
    assert "/pull/400" in comments[0]["body"]


async def test_wave_pass_record_written_after_all_green_gate(monkeypatch):
    """End-to-end (gate side): a true all-green wave-gate pass writes a
    WavePassRecord into soul memory under the canonical topic, and the
    next ``_should_skip_wave`` lookup returns True."""
    proj = "PROJ-AAAA"
    soul = _FakeSoul()
    orch = _mk_orch(kickoff={"linear_project_id": proj}, soul=soul)
    orch.poll_sleep_sec = 0
    tickets = [
        _t(f"u{i}", f"SAL-{i}", f"TIR-{i:02d}", 0, status=TicketStatus.MERGED_GREEN)
        for i in range(3)
    ]
    _seed(orch, tickets)

    async def _nosleep(delay):
        return None
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.orchestrator.asyncio.sleep", _nosleep
    )

    await orch._wait_for_wave_gate(0)
    # State events include the all-green marker.
    kinds = [e["kind"] for e in orch.state.events]
    assert "wave_all_green" in kinds

    # Exactly one wave_pass record written under the canonical topic.
    pass_writes = [
        w for w in soul.writes
        if wave_pass_topic_for(proj, 0) in (w["topics"] or [])
    ]
    assert len(pass_writes) == 1
    record = WavePassRecord.from_json(pass_writes[0]["content"])
    assert record.ratio == 1.0
    assert record.denominator == 3
    assert record.green_count == 3
    assert record.ticket_codes_seen == ["SAL-0", "SAL-1", "SAL-2"]

    # And the consumer side returns True on a freshly-written cache.
    assert await orch._should_skip_wave(0) is True
