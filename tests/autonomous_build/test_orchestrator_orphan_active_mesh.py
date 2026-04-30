"""SAL-2893 · orphan-active mesh-state reconcile tests (2026-04-30).

Reproduces the bug observed on the federation kickoff at
2026-04-30 01:35-01:55 UTC where SAL-3566/3567/3568 were left in Linear
"In Progress" by a prior crashed daemon (~23:56 UTC same day) and the
new orchestrator counted them as ``in_flight=3`` for 16+ minutes
because ``_in_flight_for_wave`` is purely status-based and the legacy
``_reconcile_orphan_active`` waited the full ``STUCK_CHILD_FORCE_FAIL_SEC``
(30 min) before force-failing.

New behaviour: when ``mesh.list_tasks(claimed)`` + ``mesh.list_tasks(pending)``
return NO task whose title carries the ticket identifier, the orphan is
provably dead and the reconciler resets it this tick (carve-out routes
``last_failure_reason="no_child_task_id"`` straight to PENDING via the
SAL-2870 phantom-child sweep).
"""

from __future__ import annotations

import pytest

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    STUCK_CHILD_FORCE_FAIL_SEC,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (kept independent of the flat suite so this file is self-contained) ──


class _FakeMesh:
    """Mesh shim returning canned ``list_tasks`` results per status.

    ``tasks_by_status`` maps "claimed"/"pending"/"completed"/"failed" to a
    list of dicts mimicking soul-svc records. Empty list = no live work.
    Set ``raise_on`` to a status name to simulate a transport failure.
    """

    def __init__(
        self,
        tasks_by_status: dict[str, list[dict]] | None = None,
        raise_on: str | None = None,
    ):
        self.tasks_by_status = tasks_by_status or {}
        self.raise_on = raise_on
        self.calls: list[tuple[str, int]] = []

    async def list_tasks(self, status=None, limit=50):
        self.calls.append((status or "", int(limit)))
        if self.raise_on and status == self.raise_on:
            raise RuntimeError(f"simulated mesh transport failure on {status}")
        return list(self.tasks_by_status.get(status or "", []))

    async def create_task(self, *, title, description="", from_session_id=None):
        return {"id": "child-x", "title": title, "status": "pending"}


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
        "id": "kick-orphan",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": "",
    }
    orch = AutonomousBuildOrchestrator(
        task=task,
        persona=_FakePersona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )
    # Bypass Linear update — orchestrator fires it as a side-effect from
    # the reconcile path; we don't care about that for these tests.
    async def _noop(*a, **kw):
        return None
    orch._update_linear_state = _noop  # type: ignore[assignment]
    return orch


def _t(uuid: str, ident: str, **kwargs) -> Ticket:
    return Ticket(
        id=uuid,
        identifier=ident,
        code=kwargs.pop("code", "OPS-01"),
        title=f"{ident} test",
        wave=kwargs.pop("wave", 1),
        epic=kwargs.pop("epic", "ops"),
        size=kwargs.pop("size", "M"),
        estimate=kwargs.pop("estimate", 5),
        is_critical_path=kwargs.pop("is_critical_path", False),
        status=kwargs.pop("status", TicketStatus.IN_PROGRESS),
        **kwargs,
    )


def _seed_graph(orch: AutonomousBuildOrchestrator, tickets: list[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


# ── Test 1: bug repro — orphan with no live mesh task is reconciled THIS tick ──


@pytest.mark.asyncio
async def test_orphan_active_no_mesh_task_reconciles_immediately():
    """Federation 2026-04-30 reproduction: SAL-3566/3567/3568 carried
    over Linear ``In Progress`` from a prior crashed daemon. The new
    orchestrator hydrated them with status=IN_PROGRESS, child_task_id=None,
    and ``_ticket_transition_ts`` seeded to ``time.time()`` (i.e.
    stuck_for ≈ 0 — well under the 30-min legacy threshold).

    Pre-fix: 16-min stall before the legacy 30-min sweep would clear
    them.

    Post-fix: mesh.list_tasks(claimed) + mesh.list_tasks(pending) return
    no task carrying ``SAL-3566/7/8`` in their titles → reconcile this
    tick.
    """
    mesh = _FakeMesh(tasks_by_status={"claimed": [], "pending": []})
    orch = _mk_orch(mesh=mesh)

    # Three orphan-active tickets (mimics the federation tonight).
    t1 = _t("u-1", "SAL-3566")
    t2 = _t("u-2", "SAL-3567")
    t3 = _t("u-3", "SAL-3568")
    _seed_graph(orch, [t1, t2, t3])

    # Sanity: stuck_for==0.0 means legacy 30-min behaviour would NOT
    # reconcile these. The fix relies entirely on mesh-state to bypass
    # the threshold.
    import time
    now = time.time()
    orch._ticket_transition_ts[t1.id] = now
    orch._ticket_transition_ts[t2.id] = now
    orch._ticket_transition_ts[t3.id] = now

    forced = await orch._reconcile_orphan_active()

    assert {t.identifier for t in forced} == {"SAL-3566", "SAL-3567", "SAL-3568"}, (
        f"expected all 3 orphans reconciled this tick; got {forced!r}"
    )
    for ticket in (t1, t2, t3):
        assert ticket.status == TicketStatus.FAILED
        assert ticket.last_failure_reason == "no_child_task_id"


# ── Test 2: orphan WITH live mesh task — legacy 30-min window applies ──


@pytest.mark.asyncio
async def test_orphan_active_with_live_mesh_task_keeps_30min_window():
    """Defensive coverage: an orphan whose identifier IS still present
    in a claimed mesh task (we lost the child_task_id but the builder is
    genuinely running) must NOT be force-failed early. The legacy 30-min
    threshold protects against killing a real builder.
    """
    # SAL-2701 has a live ``claimed`` task — the title format matches
    # ``_child_task_title`` exactly.
    live_title = (
        "[persona:alfred-coo-a] [wave-1] [ops] SAL-2701 OPS-99 — "
        "do something useful"
    )
    mesh = _FakeMesh(tasks_by_status={
        "claimed": [{"id": "live-task", "title": live_title}],
        "pending": [],
    })
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-2701", "SAL-2701")
    _seed_graph(orch, [ticket])

    # stuck_for==0 → legacy threshold definitely not crossed.
    import time
    orch._ticket_transition_ts[ticket.id] = time.time()

    forced = await orch._reconcile_orphan_active()

    assert forced == [], (
        "ticket with live mesh task must NOT be reconciled before "
        f"STUCK_CHILD_FORCE_FAIL_SEC; got {forced!r}"
    )
    assert ticket.status == TicketStatus.IN_PROGRESS


# ── Test 3: orphan with live mesh task BUT past 30-min threshold — fail ─────


@pytest.mark.asyncio
async def test_orphan_active_past_threshold_fails_even_with_live_mesh_task():
    """When ``stuck_for`` blows the legacy 30-min window, force-fail
    fires regardless of whether mesh has a live task. AB-17-x's hard
    timeout on a stuck child uses this same threshold; matching here
    keeps the two recovery paths in sync.
    """
    live_title = (
        "[persona:alfred-coo-a] [wave-2] [ops] SAL-9999 OPS-X — "
        "stuck builder"
    )
    mesh = _FakeMesh(tasks_by_status={
        "claimed": [{"id": "live-task", "title": live_title}],
        "pending": [],
    })
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-9999", "SAL-9999")
    _seed_graph(orch, [ticket])

    # Push transition_ts back past the threshold.
    import time
    orch._ticket_transition_ts[ticket.id] = (
        time.time() - STUCK_CHILD_FORCE_FAIL_SEC - 60
    )

    forced = await orch._reconcile_orphan_active()

    assert [t.identifier for t in forced] == ["SAL-9999"]
    assert ticket.status == TicketStatus.FAILED
    assert ticket.last_failure_reason == "no_child_task_id"


# ── Test 4: mesh transport failure — fall back to legacy window ─────────────


@pytest.mark.asyncio
async def test_orphan_active_mesh_transport_failure_falls_back_to_legacy():
    """If ``mesh.list_tasks`` raises (soul-svc transport blip), the
    fix MUST fall back to the legacy 30-min threshold rather than
    blindly force-failing every active ticket. Otherwise a transient
    mesh outage would stampede the whole graph into FAILED.
    """
    mesh = _FakeMesh(raise_on="claimed")
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-x", "SAL-X")
    _seed_graph(orch, [ticket])
    import time
    orch._ticket_transition_ts[ticket.id] = time.time()  # stuck_for = 0

    forced = await orch._reconcile_orphan_active()

    assert forced == [], (
        "transport failure must fall back to legacy 30-min window "
        f"(no force-fail); got {forced!r}"
    )
    assert ticket.status == TicketStatus.IN_PROGRESS


# ── Test 5: ticket WITH child_task_id is never touched here ────────────────


@pytest.mark.asyncio
async def test_orphan_active_skips_tickets_with_child_task_id():
    """The orphan-active reconciler is gated on ``child_task_id is None``
    by design — AB-17-x's reconciler in ``_poll_children`` handles
    tickets with a child id. Make sure adding mesh-state lookup didn't
    accidentally widen the gate.
    """
    mesh = _FakeMesh(tasks_by_status={"claimed": [], "pending": []})
    orch = _mk_orch(mesh=mesh)

    ticket = _t("u-c", "SAL-C", status=TicketStatus.DISPATCHED)
    ticket.child_task_id = "child-already-attached"
    _seed_graph(orch, [ticket])

    forced = await orch._reconcile_orphan_active()

    assert forced == []
    assert ticket.status == TicketStatus.DISPATCHED
    assert ticket.child_task_id == "child-already-attached"


# ── Test 6: helper unit — title substring match is identifier-based ────────


def test_ticket_has_live_mesh_task_substring_match():
    """Direct unit on the static helper. ``_child_task_title`` always
    embeds ``ticket.identifier`` verbatim; the matcher is a simple
    substring check across all active titles.
    """
    t = _t("u", "SAL-1234")

    titles_with = [
        "[persona:alfred-coo-a] [wave-1] [ops] SAL-1234 OPS-7 — work",
        "unrelated SAL-9999 task",
    ]
    titles_without = [
        "[persona:alfred-coo-a] [wave-1] [ops] SAL-9999 — other ticket",
        "no-identifier task",
    ]

    assert AutonomousBuildOrchestrator._ticket_has_live_mesh_task(t, titles_with)
    assert not AutonomousBuildOrchestrator._ticket_has_live_mesh_task(t, titles_without)
    assert not AutonomousBuildOrchestrator._ticket_has_live_mesh_task(t, [])


# ── Test 7: empty graph — no candidates, no mesh round-trip ─────────────────


@pytest.mark.asyncio
async def test_orphan_active_skips_mesh_query_when_no_candidates():
    """Performance guard: the mesh round-trip only fires when there's
    at least one orphan candidate. A graph with nothing in
    ``ACTIVE_TICKET_STATES`` (or every active ticket already has a
    child_task_id) must not pay the round-trip.
    """
    mesh = _FakeMesh(tasks_by_status={"claimed": [], "pending": []})
    orch = _mk_orch(mesh=mesh)

    # Ticket in PENDING status — not active, not a candidate.
    t = _t("u-p", "SAL-PENDING", status=TicketStatus.PENDING)
    _seed_graph(orch, [t])

    forced = await orch._reconcile_orphan_active()

    assert forced == []
    assert mesh.calls == [], (
        "mesh.list_tasks must not be called when there are no orphan "
        f"candidates; got calls={mesh.calls!r}"
    )
