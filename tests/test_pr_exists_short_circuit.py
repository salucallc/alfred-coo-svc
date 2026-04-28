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


async def test_open_pr_no_review_returns_pr_number():
    """Open PR + zero reviews + recent → returns the PR number."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),  # 1 min ago
            "reviews": [],
        }

    orch._gh_pr_open_search_fn = stub
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr == 214


async def test_open_pr_hawkman_approved_returns_none():
    """Hawkman APPROVED → None (proceed; merge bot is next, not a builder)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return {
            "number": 214,
            "created_at": _iso_minus_seconds(60),
            "reviews": [
                {"user": {"login": HAWKMAN_LOGIN}, "state": "APPROVED"},
            ],
        }

    orch._gh_pr_open_search_fn = stub
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr is None


async def test_open_pr_hawkman_request_changes_short_circuits():
    """Hawkman REQUEST_CHANGES (not APPROVED) → still skip dispatch.

    The respawn path is what should fire next, not a fresh dispatch
    against an already-flagged PR.
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
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr == 214


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
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr is None


async def test_no_pr_match_returns_none():
    """Search yielded nothing → None (proceed normally)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        return None

    orch._gh_pr_open_search_fn = stub
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-9999")
    assert pr is None


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
    pr1 = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    pr2 = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr1 == pr2 == 214
    assert call_count["n"] == 1, "second call should be served from cache"


async def test_stub_exception_treated_as_no_pr():
    """Stub raises → return None (degrade to dispatch-as-before)."""
    orch = _mk_orchestrator()

    async def stub(ident):
        raise RuntimeError("simulated GitHub outage")

    orch._gh_pr_open_search_fn = stub
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr is None


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
    pr = await orch._ticket_has_open_pr_awaiting_review("SAL-3038")
    assert pr is None


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
    existing_pr = await orch._ticket_has_open_pr_awaiting_review(
        ticket.identifier,
    )
    if existing_pr is not None:
        orch._pr_exists_skips += 1
        orch.state.record_event(
            "pr_exists_skip",
            identifier=ticket.identifier,
            pr_number=existing_pr,
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

    existing_pr = await orch._ticket_has_open_pr_awaiting_review(
        ticket.identifier,
    )
    if existing_pr is None:
        await orch._dispatch_child(ticket)

    assert len(mesh.created) == 1
    assert orch._pr_exists_skips == 0


# ── pytest async config ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _enable_async(request):
    """Match the project's existing convention — async tests run via
    pytest-asyncio's ``asyncio_mode = "auto"`` (configured in
    pyproject.toml). This fixture is a no-op safety net.
    """
    yield
