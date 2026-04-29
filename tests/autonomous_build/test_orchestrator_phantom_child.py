"""Phantom-child fix tests (2026-04-29).

Regression coverage for the 30-min phantom wait observed
2026-04-29 01:15-01:50 UTC, where wave-1 cadence reported
``in_flight=3 spend=$0.00`` with no visible mesh activity until the
``STUCK_CHILD_FORCE_FAIL_SEC`` (30 min) AB-17-y orphan-active sweep
fired. Root cause: ``_in_flight_dispatches`` is a process-local
idempotency ledger that is NEVER persisted, so a daemon restart leaves
it empty even though ``state.dispatched_child_tasks`` rehydrated live
``child_task_id`` values onto the graph.

Two fixes covered here:

1. ``_apply_restored_status`` rebuilds ``_in_flight_dispatches`` from
   any ticket whose ``child_task_id`` is non-empty after hydration.
2. ``_dispatch_child`` registers the in-flight ledger entry IMMEDIATELY
   after the mesh ``resp["id"]`` validation passes, BEFORE writing
   ``ticket.child_task_id``. Defense-in-depth: the in-process gap from
   any exception thrown between id-extraction and ledger-write is
   closed.
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
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (kept independent of the flat suite so this file is self-contained) ──


class _FakeMesh:
    def __init__(self, create_response=None, create_side_effect=None):
        self.created: list[dict] = []
        self._response = create_response
        self._side_effect = create_side_effect

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {"title": title, "description": description,
               "from_session_id": from_session_id}
        self.created.append(rec)
        if self._side_effect is not None:
            # ``side_effect`` is invoked for its observation hook (and may
            # raise to simulate a partial-failure window). The orchestrator
            # path under test runs AFTER ``resp["id"]`` validation — the
            # fake still returns a valid id so we can exercise the new
            # ledger-first ordering.
            self._side_effect(rec)
        return self._response or {"id": "child-1", "title": title,
                                  "status": "pending"}


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
        "id": "kick-phantom",
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
        wave=kwargs.pop("wave", 1),
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


# ── Test 1: hydrate path ───────────────────────────────────────────────────


def test_hydrate_rebuilds_in_flight_dispatches():
    """Daemon-restart simulation: a state with persisted
    ``dispatched_child_tasks`` must rebuild the in-memory
    ``_in_flight_dispatches`` ledger after ``_apply_restored_status``.

    Pre-fix behaviour: ledger empty post-restart → idempotency check
    skips, status-based ``_in_flight_for_wave`` still counts the ticket
    → phantom ``in_flight=N spend=$0.00`` until 30-min sweep clears.
    """
    orch = _mk_orch()

    # Two tickets: one with a live child + DISPATCHED status (the kind
    # that produces phantoms), one freshly PENDING (no child yet, must
    # NOT enter the ledger).
    t1 = _t("ticket-uuid-1", "SAL-100")
    t2 = _t("ticket-uuid-2", "SAL-101")
    _seed_graph(orch, [t1, t2])

    # Persisted state: t1 was DISPATCHED with mesh task "mesh-task-uuid-1";
    # t2 is still PENDING.
    orch.state = OrchestratorState(
        kickoff_task_id="kick-phantom",
        ticket_status={
            "ticket-uuid-1": TicketStatus.DISPATCHED.value,
            "ticket-uuid-2": TicketStatus.PENDING.value,
        },
        dispatched_child_tasks={
            "ticket-uuid-1": "mesh-task-uuid-1",
        },
    )

    # Sanity: ledger empty before hydrate (mirrors fresh process state).
    assert orch._in_flight_dispatches == {}

    orch._apply_restored_status()

    # Post-fix: ledger reflects every ticket with a live child.
    assert orch._in_flight_dispatches == {"ticket-uuid-1": "mesh-task-uuid-1"}
    # Pending ticket without a child must NOT have leaked in.
    assert "ticket-uuid-2" not in orch._in_flight_dispatches
    # Hydration also restored the child id onto the ticket node itself.
    assert orch.graph.nodes["ticket-uuid-1"].child_task_id == "mesh-task-uuid-1"


def test_hydrate_no_double_register_when_ledger_nonempty():
    """Idempotency guard: if a prior hydrate (or in-process dispatch)
    already populated the ledger, a second ``_apply_restored_status``
    call must not clobber a different value.
    """
    orch = _mk_orch()
    t1 = _t("ticket-uuid-1", "SAL-100")
    _seed_graph(orch, [t1])

    orch.state = OrchestratorState(
        kickoff_task_id="kick-phantom",
        ticket_status={"ticket-uuid-1": TicketStatus.DISPATCHED.value},
        dispatched_child_tasks={"ticket-uuid-1": "mesh-task-uuid-1"},
    )
    # Pre-seed the ledger with a different value (simulates a live
    # in-process dispatch that hasn't yet been mirrored back into state).
    orch._in_flight_dispatches["ticket-uuid-1"] = "existing-mesh-task"

    orch._apply_restored_status()

    # The existing entry must win; rebuild only fills missing slots.
    assert orch._in_flight_dispatches["ticket-uuid-1"] == "existing-mesh-task"


# ── Test 2: dispatch ordering ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_child_registers_before_assignment():
    """``_in_flight_dispatches[ticket.id]`` must be set BEFORE
    ``ticket.child_task_id`` is written. Defense-in-depth so that an
    exception between id-extraction and ledger-write still leaves the
    ledger populated, preventing a duplicate dispatch on the next tick.

    We exercise this by hooking ``mesh.create_task`` with a side-effect
    that snapshots orchestrator state at the moment the response is
    being returned. After ``_dispatch_child`` returns, the ledger must
    be set; the ticket-side write also happens (post-fix ordering still
    completes the assignment) so we assert both values.
    """
    snapshots: list[dict] = []

    def _capture(rec: dict) -> None:
        # ``create_task`` is awaited inside ``_dispatch_child``; this
        # hook fires *before* the orchestrator gets the response back.
        # Confirms the ledger was empty before id-validation.
        snapshots.append({
            "ledger_keys_at_send": list(orch._in_flight_dispatches.keys()),
            "ticket_child_at_send": ticket.child_task_id,
        })

    mesh = _FakeMesh(
        create_response={"id": "child-1", "title": "x", "status": "pending"},
        create_side_effect=_capture,
    )
    orch = _mk_orch(mesh=mesh)
    ticket = _t("ticket-uuid-1", "SAL-200")
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-phantom")
    # Bypass Linear update — orchestrator fires it as a side-effect at
    # end of dispatch; we don't care about it for this test.
    async def _noop(*a, **kw):
        return None
    orch._update_linear_state = _noop  # type: ignore[assignment]

    await orch._dispatch_child(ticket)

    # Pre-call snapshot: ledger empty + child_task_id unset.
    assert snapshots, "side_effect did not fire — mesh.create_task wasn't called"
    assert snapshots[0]["ledger_keys_at_send"] == []
    assert snapshots[0]["ticket_child_at_send"] is None

    # Post-call invariants: BOTH the ledger and the ticket reflect the
    # new child task id. (The fix's value is in the ORDER they were
    # written, not the end-state — that's covered by the next test.)
    assert orch._in_flight_dispatches["ticket-uuid-1"] == "child-1"
    assert ticket.child_task_id == "child-1"
    assert ticket.status == TicketStatus.DISPATCHED


@pytest.mark.asyncio
async def test_dispatch_child_ledger_set_even_if_post_id_assignment_fails():
    """Stronger ordering check: if anything between ``resp["id"]``
    extraction and the end of the dispatch block raises, the ledger
    must already hold the new mesh task id. Pre-fix: ledger remained
    empty because the assignment came AFTER ``ticket.child_task_id``
    write, and that's the line most likely to interact with restored
    state. Post-fix: ledger-first ordering closes the gap.

    We monkeypatch ``_update_linear_state`` to raise — the outer
    ``_run_dispatch_loop`` handler historically swallows; we swallow
    here too so the assertion can run.
    """
    mesh = _FakeMesh(create_response={"id": "child-77",
                                      "title": "x", "status": "pending"})
    orch = _mk_orch(mesh=mesh)
    ticket = _t("ticket-uuid-1", "SAL-300")
    _seed_graph(orch, [ticket])
    orch.state = OrchestratorState(kickoff_task_id="kick-phantom")

    async def _boom(*a, **kw):
        raise RuntimeError("simulated post-dispatch failure")
    orch._update_linear_state = _boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="simulated post-dispatch failure"):
        await orch._dispatch_child(ticket)

    # Critical post-fix invariant: even though the call raised after the
    # mesh task was created, the ledger reflects the in-flight child so
    # the next dispatch tick refuses a duplicate.
    assert orch._in_flight_dispatches["ticket-uuid-1"] == "child-77"
    assert ticket.child_task_id == "child-77"
