"""Inherited-open-PR review-dispatch tests (Gap 2, 2026-04-29).

Coverage for the inherited-open-PR review-fire path added to the
``_dispatch_wave`` skip-on-PR-exists branch: when an orchestrator
inherits open PRs from a prior run, ``_dispatch_review`` previously
never fired because it was only called on the builder->PR transition
path inside ``_poll_children``. Inherited PRs sat forever in a
PR-exists-skip loop. Now: in the PR-exists-skip branch, look up
whether a Hawkman QA mesh task exists for the ticket; if not, fire
one via ``_dispatch_review``. Populates ``state.review_task_ids`` so
``_poll_reviews`` picks up verdicts on the next tick.
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


# ── Fakes (mirror test_silent_with_tools_recovery.py) ─────────────────────


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
        if status == "completed":
            return []
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
        "id": "kick-inherited",
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


# ── _find_review_task_for_ticket helper ───────────────────────────────────


@pytest.mark.asyncio
async def test_find_review_task_returns_pending_match():
    """The helper returns the task id of a ``pending`` Hawkman QA task
    whose title mentions the ticket identifier.
    """
    mesh = _FakeMesh(
        pending=[
            {
                "id": "rev-1",
                "title": (
                    "[persona:hawkman-qa-a] [wave-1] [tiresias] "
                    "review SAL-3243 OPS-09 (cycle #1)"
                ),
                "status": "pending",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)
    found = await orch._find_review_task_for_ticket("SAL-3243")
    assert found == "rev-1"


@pytest.mark.asyncio
async def test_find_review_task_returns_claimed_match():
    """Claimed Hawkman QA tasks are surfaced too — a worker has picked
    up the review but it hasn't completed yet.
    """
    mesh = _FakeMesh(
        claimed=[
            {
                "id": "rev-claimed-2",
                "title": (
                    "[persona:hawkman-qa-a] [wave-0] [ops] "
                    "review SAL-9999 OPS-14C (cycle #1)"
                ),
                "status": "claimed",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)
    found = await orch._find_review_task_for_ticket("SAL-9999")
    assert found == "rev-claimed-2"


@pytest.mark.asyncio
async def test_find_review_task_returns_none_when_no_match():
    """No Hawkman QA task matches the ticket identifier -> None so the
    caller can fire a fresh review.
    """
    mesh = _FakeMesh(
        pending=[
            {
                "id": "rev-other",
                "title": (
                    "[persona:hawkman-qa-a] [wave-1] [tiresias] "
                    "review SAL-1234 OPS-01 (cycle #1)"
                ),
                "status": "pending",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)
    found = await orch._find_review_task_for_ticket("SAL-3243")
    assert found is None


@pytest.mark.asyncio
async def test_find_review_task_ignores_non_hawkman_persona():
    """A pending task that mentions the ticket but is not a Hawkman QA
    task (e.g. a builder claim) must not be returned.
    """
    mesh = _FakeMesh(
        pending=[
            {
                "id": "build-task",
                "title": (
                    "[persona:alfred-coo-a] [wave-1] [tiresias] "
                    "SAL-3243 OPS-09 build"
                ),
                "status": "pending",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)
    found = await orch._find_review_task_for_ticket("SAL-3243")
    assert found is None


# ── Inherited-open-PR branch behaviour ────────────────────────────────────


@pytest.mark.asyncio
async def test_inherited_pr_no_existing_review_fires_dispatch(monkeypatch):
    """Inherited PR + no Hawkman QA task in mesh + ticket NOT in
    ``state.review_task_ids`` -> orchestrator fires ``_dispatch_review``
    and registers the new task id in ``state.review_task_ids``.
    """
    mesh = _FakeMesh()  # empty mesh: no pending/claimed reviews
    orch = _mk_orch(mesh=mesh)
    orch._gh_pr_open_search_fn = AsyncMock(
        return_value={"number": 279, "created_at": None, "reviews": []},
    )

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=1, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")

    dispatch_review_mock = AsyncMock(
        side_effect=lambda ticket: orch.state.review_task_ids.__setitem__(
            ticket.id, "review-task-1",
        ),
    )
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    dispatch_review_mock.assert_awaited_once_with(t)
    assert orch.state.review_task_ids[t.id] == "review-task-1"


@pytest.mark.asyncio
async def test_inherited_pr_existing_mesh_task_no_duplicate_dispatch(
    monkeypatch,
):
    """Inherited PR + existing claimed Hawkman QA task in mesh ->
    orchestrator does NOT fire a duplicate review; just registers the
    pre-existing task in ``state.review_task_ids``.
    """
    mesh = _FakeMesh(
        claimed=[
            {
                "id": "pre-existing-rev",
                "title": (
                    "[persona:hawkman-qa-a] [wave-1] [tiresias] "
                    "review SAL-3243 OPS-09 (cycle #1)"
                ),
                "status": "claimed",
            },
        ],
    )
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=1, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")

    dispatch_review_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    dispatch_review_mock.assert_not_awaited()
    assert orch.state.review_task_ids[t.id] == "pre-existing-rev"
    assert t.review_task_id == "pre-existing-rev"


@pytest.mark.asyncio
async def test_inherited_pr_already_in_state_idempotent(monkeypatch):
    """Inherited PR + ticket already in ``state.review_task_ids`` ->
    orchestrator does NOT fire (idempotent) and does NOT re-query the
    mesh. Pins double-mesh-call avoidance for the hot dispatch path.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=1, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")
    orch.state.review_task_ids[t.id] = "already-registered"

    dispatch_review_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)
    find_mock = AsyncMock()
    monkeypatch.setattr(orch, "_find_review_task_for_ticket", find_mock)

    await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    dispatch_review_mock.assert_not_awaited()
    find_mock.assert_not_awaited()
    assert orch.state.review_task_ids[t.id] == "already-registered"


@pytest.mark.asyncio
async def test_inherited_pr_dispatch_review_failure_swallowed(monkeypatch):
    """If ``_dispatch_review`` raises (e.g. mesh outage), the inherited-
    PR branch must swallow the exception and fall back to the original
    skip-only behaviour. The ticket stays in PENDING; the dispatch loop
    will retry on the next tick.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=1, epic="tiresias")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")

    dispatch_review_mock = AsyncMock(side_effect=RuntimeError("mesh down"))
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    # Must not raise.
    await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    dispatch_review_mock.assert_awaited_once_with(t)
    assert t.id not in orch.state.review_task_ids


# ── Regression: builder->PR transition still fires _dispatch_review ───────


@pytest.mark.asyncio
async def test_builder_to_pr_transition_still_fires_review(monkeypatch):
    """Regression: the existing builder->PR transition path inside
    ``_poll_children`` still fires ``_dispatch_review`` correctly. We
    drive a completed child task with a PR URL through ``_poll_children``
    and assert the review dispatch was awaited. This pins the original
    behaviour against future refactors of the inherited-PR branch.
    """
    mesh = _FakeMesh()

    # Inject a completed child carrying a PR URL into the live mesh poll.
    async def _list_tasks(status=None, limit=50):
        if status == "completed":
            return [
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
            ]
        return []

    mesh.list_tasks = _list_tasks  # type: ignore[assignment]
    orch = _mk_orch(mesh=mesh)

    t = _t("u-2999", "SAL-2999", code="OPS-01")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-builder-1"
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")

    dispatch_review_mock = AsyncMock()
    monkeypatch.setattr(orch, "_dispatch_review", dispatch_review_mock)

    async def _fake_update_linear(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update_linear)

    await orch._poll_children()

    dispatch_review_mock.assert_awaited_once()
    awaited_ticket = dispatch_review_mock.call_args.args[0]
    assert awaited_ticket is t
    assert t.pr_url == "https://github.com/salucallc/foo/pull/100"


# ── Defensive: missing ticket / pr ────────────────────────────────────────


@pytest.mark.asyncio
async def test_inherited_pr_without_pr_url_synthesizes_one(monkeypatch):
    """The inherited-PR helper synthesizes a placeholder ``pr_url`` from
    the PR number when the ticket doesn't already carry one (typical for
    a freshly-rehydrated ticket whose prior orchestrator run never wrote
    ``state.pr_urls`` for this PR). Defensive: ``_dispatch_review``
    interpolates ``ticket.pr_url`` into the body, so it must be a non-
    empty string.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)

    t = _t("u-3243", "SAL-3243", code="OPS-09", wave=1, epic="tiresias")
    t.status = TicketStatus.PENDING
    t.pr_url = None
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-inherited")

    captured = {}

    async def _capture_dispatch(ticket):
        captured["pr_url"] = ticket.pr_url
        orch.state.review_task_ids[ticket.id] = "review-task-1"

    monkeypatch.setattr(orch, "_dispatch_review", _capture_dispatch)

    await orch._fire_review_for_inherited_pr(t, existing_pr=279)

    assert captured["pr_url"] is not None
    assert "279" in captured["pr_url"]
