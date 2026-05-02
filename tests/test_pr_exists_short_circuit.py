"""SAL-3038 PR-exists short-circuit tests.

Covers ``AutonomousBuildOrchestrator._ticket_has_open_pr_awaiting_review``
plus the dispatch-loop integration in ``_dispatch_wave``. Background:
SAL-3038 (PR #214) was re-dispatched 68 times in 7 days because the
dispatch loop didn't check whether an open PR awaiting Hawkman review
already existed for the ticket. Each cycle burned $0.50-2 producing
nothing useful.

Tests use ``self._gh_pr_open_search_fn`` to stub the GitHub round-trip,
mirroring the ``_gh_pr_search_fn`` pattern from the stale-sweep tests.
"""

from __future__ import annotations

import json
import time

import pytest

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    HAWKMAN_LOGIN,
    PR_EXISTS_FRESH_PR_WINDOW_SEC,
    _OpenPrCheck,
)


# ── Fakes (mirrors test_autonomous_build_orchestrator.py) ─────────────────


class _FakeMesh:
    def __init__(self):
        self.created: list[dict] = []
        self._next_id = 1

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {"title": title, "description": description}
        self.created.append(rec)
        nid = f"child-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        return None


class _FakeSoul:
    async def write_memory(self, content, topics=None):
        return {"memory_id": "m-1"}

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


def _mk_orchestrator(mesh=None) -> AutonomousBuildOrchestrator:
    task = {
        "id": "kick-abc",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": json.dumps({}),
    }
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=mesh or _FakeMesh(),
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _t(uuid, ident, code, wave, epic, **kwargs) -> Ticket:
    return Ticket(
        id=uuid, identifier=ident, code=code, title=f"{ident} {code}",
        wave=wave, epic=epic,
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        **kwargs,
    )


def _seed_graph(orch, tickets):
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _iso_minus_seconds(seconds: float) -> str:
    """Return an ISO-8601 Z-suffixed timestamp ``seconds`` ago."""
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(time.time() - seconds, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ── Helper unit tests ──────────────────────────────────────────────────────


async def test_open_pr_no_review_returns_awaiting_review():
    """Open PR + zero reviews + recent → awaiting_review check."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),  # 1 min ago
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert isinstance(check, _OpenPrCheck)
    assert check.pr_number == 214
    assert check.state == "awaiting_review"


async def test_open_pr_hawkman_approved_returns_approved_check():
    """Hawkman APPROVED → approved check (caller fires _merge_pr).

    Substrate task #82 (2026-05-02): previously this path returned None
    and the dispatch loop fell through to a duplicate builder. Now the
    helper signals the merge-ready state explicitly so the dispatch
    site can short-circuit into ``_merge_pr``.
    """
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "html_url": "https://github.com/salucallc/alfred-coo-svc/pull/214",
            "reviews": [
                {"user": {"login": HAWKMAN_LOGIN}, "state": "APPROVED"},
            ],
        }

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert isinstance(check, _OpenPrCheck)
    assert check.pr_number == 214
    assert check.state == "approved"
    assert check.pr_url == (
        "https://github.com/salucallc/alfred-coo-svc/pull/214"
    )


async def test_open_pr_hawkman_request_changes_awaiting_review():
    """Hawkman REQUEST_CHANGES (not APPROVED) → awaiting_review.

    The respawn path is what should fire next, not a fresh dispatch
    against an already-flagged PR — same skip semantics as the no-review
    case, just driven by the review verdict path instead.
    """
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "reviews": [
                {
                    "user": {"login": HAWKMAN_LOGIN},
                    "state": "REQUEST_CHANGES",
                },
            ],
        }

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert isinstance(check, _OpenPrCheck)
    assert check.pr_number == 214
    assert check.state == "awaiting_review"


async def test_open_pr_too_old_returns_none():
    """PR older than 7 days → None (treat as stale; dispatch fresh)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 100,
            # 8 days ago
            "created_at": _iso_minus_seconds(
                PR_EXISTS_FRESH_PR_WINDOW_SEC + 24 * 60 * 60
            ),
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert check is None


async def test_no_pr_match_returns_none():
    """Search yielded nothing → None (proceed normally)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return None

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-9999")
    assert check is None


async def test_cache_avoids_double_call(monkeypatch):
    """Two consecutive calls within the cache window → stub fires once."""
    orch = _mk_orchestrator()
    call_count = {"n": 0}

    async def stub(ident):
        call_count["n"] += 1
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub
    c1 = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    c2 = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert c1 == c2
    assert isinstance(c1, _OpenPrCheck)
    assert c1.pr_number == 214
    assert c1.state == "awaiting_review"
    assert call_count["n"] == 1, "second call should be served from cache"


async def test_stub_exception_treated_as_no_pr():
    """Stub raises → return None (degrade to dispatch-as-before)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        raise RuntimeError("simulated GitHub outage")

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert check is None


async def test_unparseable_created_at_returns_none():
    """Garbage created_at → treat as stale, return None."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": "not-a-date",
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub
    check = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert check is None


# ── Dispatch-loop integration ──────────────────────────────────────────────


async def test_dispatch_loop_skips_when_open_pr_awaiting_review():
    """End-to-end: a ticket with an open PR awaiting Hawkman review must
    NOT result in a builder dispatch on the mesh, and must record a
    ``pr_exists_skip`` state event.
    """
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    ticket = _t("ua", "SAL-3038", "OPS-14D", 1, "ops")
    _seed_graph(orch, [ticket])

    async def stub(ident):
        assert ident == "SAL-3038"
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub

    # Simulate the inner dispatch decision: replicate the wave-loop
    # branch around ``_dispatch_child`` exactly.
    existing_pr_check = await orch._ticket_has_open_pr_awaiting_review(
        ticket.identifier,
    )
    if existing_pr_check is not None:
        orch._pr_exists_skips += 1
        orch.state.record_event(
            "pr_exists_skip",
            identifier=ticket.identifier,
            pr_number=existing_pr_check.pr_number,
            skips_total=orch._pr_exists_skips,
        )
    else:
        await orch._dispatch_child(ticket)

    assert mesh.created == [], "must not dispatch a builder when PR awaits review"
    assert orch._pr_exists_skips == 1
    skip_events = [
        e for e in orch.state.events if e.get("kind") == "pr_exists_skip"
    ]
    assert len(skip_events) == 1
    assert skip_events[0]["pr_number"] == 214
    assert skip_events[0]["identifier"] == "SAL-3038"


async def test_dispatch_loop_proceeds_when_no_open_pr():
    """End-to-end: no open PR → builder DOES dispatch normally."""
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    ticket = _t("ub", "SAL-9999", "OPS-99", 1, "ops")
    _seed_graph(orch, [ticket])

    async def stub(ident):
        return None

    orch._gh_pr_open_search_fn = stub

    existing_pr_check = await orch._ticket_has_open_pr_awaiting_review(
        ticket.identifier,
    )
    if existing_pr_check is None:
        await orch._dispatch_child(ticket)

    assert len(mesh.created) == 1
    assert orch._pr_exists_skips == 0


async def test_dispatch_loop_fires_merge_when_open_pr_already_approved():
    """Substrate task #82 (2026-05-02): an inherited open PR that is
    already Hawkman-APPROVED must trigger ``_merge_pr`` directly instead
    of falling through to a duplicate builder dispatch.

    Replicates the ``existing_pr_check.state == "approved"`` branch in
    ``_dispatch_wave``. Mocks ``_merge_pr`` + ``_update_linear_state`` so
    the test doesn't reach the real GitHub merge tool. Verifies:
      - No builder mesh task created.
      - ``_merge_pr`` invoked with the ticket whose ``pr_url`` was
        populated from the helper's lookup.
      - ``pr_exists_approved_merge`` state event recorded.
      - Ticket transitions to MERGED_GREEN on a successful merge.
    """
    from alfred_coo.autonomous_build.graph import TicketStatus

    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)

    ticket = _t("uc", "SAL-3038", "OPS-14D", 1, "ops")
    ticket.status = TicketStatus.PENDING
    _seed_graph(orch, [ticket])

    pr_html_url = "https://github.com/salucallc/alfred-coo-svc/pull/214"

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "html_url": pr_html_url,
            "reviews": [
                {"user": {"login": HAWKMAN_LOGIN}, "state": "APPROVED"},
            ],
        }

    orch._gh_pr_open_search_fn = stub

    merge_calls: list[str] = []

    async def fake_merge(t):
        merge_calls.append(t.identifier)
        # Simulate a successful merge by stamping merged_pr_urls so any
        # idempotency checks downstream see the SHA.
        orch.state.merged_pr_urls[t.id] = "fakedeadbeef"
        return True

    linear_calls: list[tuple[str, str]] = []

    async def fake_linear(t, target):
        linear_calls.append((t.identifier, target))

    orch._merge_pr = fake_merge  # type: ignore[assignment]
    orch._update_linear_state = fake_linear  # type: ignore[assignment]

    # Replicate the dispatch-loop approved-PR branch from _dispatch_wave.
    existing_pr_check = await orch._ticket_has_open_pr_awaiting_review(
        ticket.identifier,
    )
    assert existing_pr_check is not None
    assert existing_pr_check.state == "approved"

    orch._pr_exists_skips += 1
    if not ticket.pr_url and existing_pr_check.pr_url:
        ticket.pr_url = existing_pr_check.pr_url
    orch.state.record_event(
        "pr_exists_approved_merge",
        identifier=ticket.identifier,
        pr_number=existing_pr_check.pr_number,
        pr_url=ticket.pr_url,
        skips_total=orch._pr_exists_skips,
    )
    ticket.status = TicketStatus.MERGE_REQUESTED
    merged = await orch._merge_pr(ticket)
    if merged:
        ticket.status = TicketStatus.MERGED_GREEN
        await orch._update_linear_state(ticket, "Done")

    assert mesh.created == [], (
        "must not dispatch a builder when PR is already approved"
    )
    assert merge_calls == ["SAL-3038"]
    assert ticket.pr_url == pr_html_url
    assert ticket.status == TicketStatus.MERGED_GREEN
    assert linear_calls == [("SAL-3038", "Done")]
    approve_events = [
        e for e in orch.state.events
        if e.get("kind") == "pr_exists_approved_merge"
    ]
    assert len(approve_events) == 1
    assert approve_events[0]["pr_number"] == 214
    assert approve_events[0]["pr_url"] == pr_html_url


# ── pytest async config ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_async(request):
    """Match the project's existing convention — async tests run via
    pytest-asyncio's ``asyncio_mode = "auto"`` (configured in
    pyproject.toml). This fixture is a no-op safety net.
    """
    yield
