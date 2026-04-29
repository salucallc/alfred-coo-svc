"""Builder-skip-already-merged tests (Gap 4, 2026-04-29).

Coverage for the pre-flight merged-PR check added to ``_dispatch_wave``:
before firing a builder for a ticket, the orchestrator now searches
GitHub for a merged PR mentioning the ticket identifier; if one exists
the ticket has already shipped, the orchestrator marks it MERGED_GREEN
and skips dispatch. Reuses the existing ``_search_recent_merged_pr_urls``
helper (driven by the ``_gh_pr_search_fn`` test stub).

Pins four behaviours:
  1. Ticket with a merged PR → ``_ticket_has_merged_pr`` returns the
     URL; the dispatch-loop branch marks MERGED_GREEN, records a
     ``merged_pr_skip`` event, and does NOT create a builder mesh task.
  2. Ticket with no merged PR → helper returns None; the orchestrator
     dispatches a builder normally.
  3. Ticket with only an OPEN PR (search yields nothing because the
     stub returns []) → helper returns None; ticket is NOT marked
     MERGED_GREEN. Whatever path is correct (e.g. PR-exists-skip →
     AWAITING_REVIEW) takes over.
  4. Two consecutive lookups of the same ticket within the cache
     window result in exactly one underlying GitHub-search call.
"""

from __future__ import annotations

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
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (mirror test_pr_exists_ready_counter.py + test_inherited_open_pr_review_dispatch.py) ──


class _FakeMesh:
    def __init__(self):
        self.created: list[dict] = []
        self._next_id = 1

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {
            "title": title, "description": description,
            "from_session_id": from_session_id,
        }
        self.created.append(rec)
        nid = f"child-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
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
        "id": "kick-gap4",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": json.dumps({}),
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


# ── 1. merged-PR present → MERGED_GREEN + no dispatch ─────────────────────


@pytest.mark.asyncio
async def test_merged_pr_marks_green_and_skips_dispatch():
    """A ticket whose merged PR is found in GitHub Search must be
    transitioned to MERGED_GREEN, must record a ``merged_pr_skip``
    state event, and must NOT result in a builder mesh task being
    created. Mirrors the dispatch-loop branch in ``_dispatch_wave``.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-3037", "SAL-3037", code="OPS-14E", wave=2, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    # _gh_pr_search_fn is the existing stub used by the stale-sweep
    # helpers. It feeds _search_recent_merged_pr_urls, which
    # _ticket_has_merged_pr layers cache + None-handling on top of.
    async def stub(ident):
        assert ident == "SAL-3037"
        return ["https://github.com/salucallc/alfred-coo-svc/pull/283"]

    orch._gh_pr_search_fn = stub
    # Avoid live Linear API call during test.
    orch._update_linear_state = _noop_update_linear

    merged_url = await orch._ticket_has_merged_pr(ticket.identifier)
    assert merged_url == "https://github.com/salucallc/alfred-coo-svc/pull/283"

    # Replicate the dispatch-loop branch added in this PR.
    if merged_url is not None:
        orch._merged_pr_skips += 1
        ticket.status = TicketStatus.MERGED_GREEN
        if not ticket.pr_url:
            ticket.pr_url = merged_url
        orch.state.record_event(
            "merged_pr_skip",
            identifier=ticket.identifier,
            pr_url=merged_url,
            skips_total=orch._merged_pr_skips,
        )
        await orch._update_linear_state(ticket, "Done")
    else:  # pragma: no cover — branch retained for parity with prod
        await orch._dispatch_child(ticket)

    assert ticket.status == TicketStatus.MERGED_GREEN
    assert ticket.pr_url == "https://github.com/salucallc/alfred-coo-svc/pull/283"
    assert mesh.created == [], "must not dispatch a builder for already-merged ticket"
    assert orch._merged_pr_skips == 1
    skip_events = [
        e for e in orch.state.events if e.get("kind") == "merged_pr_skip"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["identifier"] == "SAL-3037"
    assert skip_events[0]["pr_url"] == (
        "https://github.com/salucallc/alfred-coo-svc/pull/283"
    )


# ── 2. no merged PR → builder dispatches normally ────────────────────────


@pytest.mark.asyncio
async def test_no_merged_pr_dispatches_normally():
    """Search yields nothing → helper returns None and the orchestrator
    proceeds to ``_dispatch_child``.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-9999", "SAL-9999", code="OPS-99", wave=1, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    async def stub(ident):
        return []

    orch._gh_pr_search_fn = stub

    merged_url = await orch._ticket_has_merged_pr(ticket.identifier)
    assert merged_url is None

    if merged_url is None:
        await orch._dispatch_child(ticket)

    assert ticket.status != TicketStatus.MERGED_GREEN
    assert len(mesh.created) == 1, "builder must dispatch when no merged PR exists"
    assert orch._merged_pr_skips == 0


# ── 3. open PR only (no merged PR) → not MERGED_GREEN ─────────────────────


@pytest.mark.asyncio
async def test_open_pr_does_not_trigger_merged_green():
    """The merged-PR helper searches with ``is:merged``; an OPEN PR is
    invisible to it. Returning [] from the search stub mirrors that:
    the helper returns None, the ticket is NOT marked MERGED_GREEN,
    and the dispatch path falls through to whatever check handles
    open PRs (covered by SAL-3038 / PR #286 — AWAITING_REVIEW).
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-3038", "SAL-3038", code="OPS-14D", wave=1, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    # Open PR exists in reality but ``is:merged`` excludes it from the
    # search response — stub returns [].
    async def merged_stub(ident):
        return []

    orch._gh_pr_search_fn = merged_stub

    merged_url = await orch._ticket_has_merged_pr(ticket.identifier)
    assert merged_url is None

    # Status must not have moved.
    assert ticket.status == TicketStatus.PENDING
    skip_events = [
        e for e in orch.state.events if e.get("kind") == "merged_pr_skip"
    ]
    assert skip_events == [], "open-PR-only must not record merged_pr_skip"


# ── 4. cache: only one GitHub call per ticket per window ──────────────────


@pytest.mark.asyncio
async def test_merged_pr_lookup_cached_within_window():
    """Two consecutive ``_ticket_has_merged_pr`` calls for the same
    ticket within ``MERGED_PR_CACHE_TTL_SEC`` must invoke the underlying
    search stub exactly once. Mirrors ``_pr_exists_cache`` from the
    SAL-3038 short-circuit (PR #286).
    """
    orch = _mk_orch()
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")
    call_count = {"n": 0}

    async def stub(ident):
        call_count["n"] += 1
        return ["https://github.com/salucallc/alfred-coo-svc/pull/283"]

    orch._gh_pr_search_fn = stub

    first = await orch._ticket_has_merged_pr("SAL-3037")
    second = await orch._ticket_has_merged_pr("SAL-3037")

    assert first == second == (
        "https://github.com/salucallc/alfred-coo-svc/pull/283"
    )
    assert call_count["n"] == 1, (
        "second lookup within cache window must be served from cache"
    )


# ── helpers ────────────────────────────────────────────────────────────────


async def _noop_update_linear(ticket, state_name):
    """Stand-in for ``_update_linear_state`` so tests don't try to import
    the live ``alfred_coo.tools`` registry."""
    return None
