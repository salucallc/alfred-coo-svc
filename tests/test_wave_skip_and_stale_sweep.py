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

    async def fake_pr_search(ident: str):
        if ident == "SAL-2656":
            return "https://github.com/salucallc/alfred-coo-svc/pull/145"
        return None
    orch._gh_pr_search_fn = fake_pr_search

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
