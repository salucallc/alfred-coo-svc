"""Builder-skip-already-merged tests (Gap 4, 2026-04-29).

Coverage for the pre-flight merged-PR check added to ``_dispatch_wave``:
before firing a builder for a ticket, the orchestrator searches GitHub
for a merged PR mentioning the ticket identifier; if one exists AND the
PR's changed files intersect the ticket's ``_TARGET_HINTS`` expected
scope, the ticket has already shipped, the orchestrator marks it
MERGED_GREEN and skips dispatch. Reuses the ``_search_recent_merged_pr_urls``
+ ``_pr_intersects_expected_scope`` helpers (driven by the ``_gh_pr_search_fn``
and ``_gh_pr_files_fn`` test stubs).

2026-04-29 federation-misfire fix: the path-intersection check is now
shared with ``_find_recent_merged_pr_for`` so the dispatch loop and
stale-sweep agree on what counts as evidence-of-implementation. Tests
below assert the behaviour at the helper level and at the dispatch-loop
branch level.

Pins six behaviours:
  1. Ticket WITH ``_TARGET_HINTS`` + PR that touches an expected path
     -> MERGED_GREEN, builder NOT dispatched.
  2. Ticket WITH ``_TARGET_HINTS`` + PR that only mentions the ticket
     ID but doesn't touch expected paths -> helper returns None,
     ticket dispatches fresh (federation 02:33Z misfire regression).
  3. Ticket WITHOUT ``_TARGET_HINTS`` (NO_HINT) + PR that mentions the
     ticket ID -> helper returns None, ticket dispatches fresh
     (conservative: refuse to auto-flip on weak evidence).
  4. No merged PR found at all -> helper returns None, dispatch
     proceeds normally.
  5. Open PR only (search returns []) -> helper returns None, ticket
     not marked MERGED_GREEN.
  6. Two consecutive lookups of the same ticket within the cache
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


# Fakes mirror test_pr_exists_ready_counter.py + test_inherited_open_pr_review_dispatch.py


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


# 1. merged-PR present + path intersects -> MERGED_GREEN + no dispatch


@pytest.mark.asyncio
async def test_merged_pr_with_intersecting_files_marks_green_and_skips_dispatch():
    """A ticket whose merged PR is found in GitHub Search AND whose
    changed files intersect the ticket's ``_TARGET_HINTS`` expected
    scope must be transitioned to MERGED_GREEN, must record a
    ``merged_pr_skip`` state event, and must NOT result in a builder
    mesh task being created. Mirrors the dispatch-loop branch in
    ``_dispatch_wave``.

    Uses OPS-14D which has ``scope_middleware.py`` in
    ``_TARGET_HINTS[OPS-14D].paths`` so the PR's changed files can
    intersect.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-3037", "SAL-3037", code="OPS-14D", wave=2, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    async def search_stub(ident):
        assert ident == "SAL-3037"
        return ["https://github.com/salucallc/alfred-coo-svc/pull/283"]

    async def files_stub(url):
        # PR #283 actually edits the OPS-14D scope_middleware implementation
        # file -> intersects expected scope -> counts as evidence.
        assert url == "https://github.com/salucallc/alfred-coo-svc/pull/283"
        return ("src/alfred_coo/auth/scope_middleware.py",)

    orch._gh_pr_search_fn = search_stub
    orch._gh_pr_files_fn = files_stub
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
    else:  # pragma: no cover - branch retained for parity with prod
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


# 2. PR mentions ticket but doesn't touch expected paths -> DISPATCH FRESH


@pytest.mark.asyncio
async def test_merged_pr_without_intersecting_files_dispatches_fresh():
    """Federation 2026-04-29 02:33:25Z misfire regression: a merged PR
    that mentions a ticket ID in title or body but does NOT touch any
    of the ticket's ``_TARGET_HINTS`` expected paths must NOT be treated
    as evidence-of-implementation. The helper returns None and the
    dispatch loop fires a builder fresh.

    Concretely: SAL-3568 (federation wave-1) had hint paths under
    ``migrations/`` but the falsely-claimed PR #302 only touched
    unrelated files (it mentioned SAL-3568 in its body for context).
    The stale-sweep matcher already rejected this; the dispatch-loop
    pre-flight previously did not.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    # OPS-14D has paths under src/alfred_coo/auth/ in _TARGET_HINTS
    ticket = _t("u-fed", "SAL-3568", code="OPS-14D", wave=1, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    async def search_stub(ident):
        return ["https://github.com/salucallc/alfred-coo-svc/pull/302"]

    async def files_stub(url):
        # PR #302 touches unrelated migrations files; mentions SAL-3568
        # in body but does NOT implement OPS-14D.
        return (
            "migrations/mssp_federation_functions.sql",
            "migrations/mssp_federation_tables.sql",
        )

    orch._gh_pr_search_fn = search_stub
    orch._gh_pr_files_fn = files_stub

    merged_url = await orch._ticket_has_merged_pr(ticket.identifier)
    assert merged_url is None, (
        "PR that mentions ticket but doesn't intersect expected scope "
        "must NOT be treated as evidence-of-implementation"
    )

    # Replicate the dispatch-loop branch: helper returned None -> dispatch.
    if merged_url is None:
        await orch._dispatch_child(ticket)

    assert ticket.status != TicketStatus.MERGED_GREEN
    assert len(mesh.created) == 1, (
        "builder must dispatch when merged PR is not the implementation"
    )
    assert orch._merged_pr_skips == 0


# 3. NO_HINT ticket + PR mentions it -> DISPATCH FRESH (conservative)


@pytest.mark.asyncio
async def test_no_hint_ticket_with_merged_pr_dispatches_fresh():
    """A ticket whose ``code`` is not in ``_TARGET_HINTS`` cannot be
    scope-verified, so the helper conservatively returns None - the
    dispatch loop fires a builder fresh rather than trusting a
    title-mention. Mirrors the stale-sweep matcher's NO_HINT branch
    (refuse to auto-flip on weak evidence).
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    # OPS-14E is intentionally NOT in _TARGET_HINTS (NO_HINT case).
    ticket = _t("u-9999", "SAL-9999", code="OPS-14E", wave=1, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    async def search_stub(ident):
        return ["https://github.com/salucallc/alfred-coo-svc/pull/999"]

    async def files_stub(url):  # pragma: no cover - should never be called
        raise AssertionError(
            "files_stub must not be called when ticket has no _TARGET_HINTS"
        )

    orch._gh_pr_search_fn = search_stub
    orch._gh_pr_files_fn = files_stub

    merged_url = await orch._ticket_has_merged_pr(ticket.identifier)
    assert merged_url is None, (
        "NO_HINT ticket must not auto-flip on title-mention alone"
    )

    if merged_url is None:
        await orch._dispatch_child(ticket)

    assert ticket.status != TicketStatus.MERGED_GREEN
    assert len(mesh.created) == 1, (
        "NO_HINT ticket must dispatch fresh; conservative refusal to skip"
    )
    assert orch._merged_pr_skips == 0


# 4. no merged PR -> builder dispatches normally


@pytest.mark.asyncio
async def test_no_merged_pr_dispatches_normally():
    """Search yields nothing -> helper returns None and the orchestrator
    proceeds to ``_dispatch_child``.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-9999", "SAL-9999", code="OPS-14D", wave=1, epic="ops")
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


# 5. open PR only (no merged PR) -> not MERGED_GREEN


@pytest.mark.asyncio
async def test_open_pr_does_not_trigger_merged_green():
    """The merged-PR helper searches with ``is:merged``; an OPEN PR is
    invisible to it. Returning [] from the search stub mirrors that:
    the helper returns None, the ticket is NOT marked MERGED_GREEN,
    and the dispatch path falls through to whatever check handles
    open PRs (covered by SAL-3038 / PR #286 - AWAITING_REVIEW).
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-3038", "SAL-3038", code="OPS-14D", wave=1, epic="ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")

    # Open PR exists in reality but ``is:merged`` excludes it from the
    # search response - stub returns [].
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


# 6. cache: only one GitHub call per ticket per window


@pytest.mark.asyncio
async def test_merged_pr_lookup_cached_within_window():
    """Two consecutive ``_ticket_has_merged_pr`` calls for the same
    ticket within ``MERGED_PR_CACHE_TTL_SEC`` must invoke the underlying
    search stub exactly once. Mirrors ``_pr_exists_cache`` from the
    SAL-3038 short-circuit (PR #286).
    """
    orch = _mk_orch()
    orch.state = OrchestratorState(kickoff_task_id="kick-gap4")
    # Seed graph so the path-intersection check resolves a hint.
    ticket = _t("u-3037", "SAL-3037", code="OPS-14D", wave=2, epic="ops")
    _seed_graph(orch, [ticket])

    search_count = {"n": 0}
    files_count = {"n": 0}

    async def search_stub(ident):
        search_count["n"] += 1
        return ["https://github.com/salucallc/alfred-coo-svc/pull/283"]

    async def files_stub(url):
        files_count["n"] += 1
        return ("src/alfred_coo/auth/scope_middleware.py",)

    orch._gh_pr_search_fn = search_stub
    orch._gh_pr_files_fn = files_stub

    first = await orch._ticket_has_merged_pr("SAL-3037")
    second = await orch._ticket_has_merged_pr("SAL-3037")

    assert first == second == (
        "https://github.com/salucallc/alfred-coo-svc/pull/283"
    )
    assert search_count["n"] == 1, (
        "second lookup within cache window must be served from cache"
    )
    assert files_count["n"] == 1, (
        "files lookup must also be cached via the merged-pr cache"
    )


# helpers


async def _noop_update_linear(ticket, state_name):
    """Stand-in for ``_update_linear_state`` so tests don't try to import
    the live ``alfred_coo.tools`` registry."""
    return None
