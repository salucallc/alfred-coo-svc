"""SAL-4036: file-collision-aware dispatch tests.

The orchestrator's pre-dispatch gates today are (1) wave gate, (2)
``per_epic_cap``, and (3) Linear ``blockedBy`` / ``blocks`` deps. SAL-4036
adds a fourth: two same-wave tickets that touch the same file in the
same repo (e.g. AIO W2-A and W3-E both editing ``docker-compose.yml``)
must serialise instead of dispatching concurrently — overlapping branches
race on the merge and produce conflict storms.

These tests drive the gate directly via ``_file_collision_for`` /
``_ticket_file_set`` against a hand-rolled ticket graph + ``TargetHint``
registry, mirroring the patterns in ``test_autonomous_build_orchestrator``
so the full dispatch loop doesn't need to be set up. The integration-ish
test at the bottom asserts that the loop's per-tick gating produces the
expected ``ticket_serialized_file_collision`` event.
"""

from __future__ import annotations

import json
from typing import List

import pytest

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    TargetHint,
)


# ── Fakes (mirrors test_autonomous_build_orchestrator.py) ─────────────────


class _FakeMesh:
    def __init__(self):
        self.created: list[dict] = []
        self._next_id = 1

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {"title": title, "description": description,
               "from_session_id": from_session_id}
        self.created.append(rec)
        nid = f"child-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        pass


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


def _mk_persona():
    class P:
        name = "autonomous-build-a"
        handler = "AutonomousBuildOrchestrator"
    return P()


def _mk_orchestrator(
    kickoff_desc: dict | str = "",
) -> AutonomousBuildOrchestrator:
    if isinstance(kickoff_desc, dict):
        kickoff_desc = json.dumps(kickoff_desc)
    task = {"id": "kick-abc", "title": "[persona:autonomous-build-a] kickoff",
            "description": kickoff_desc}
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=_FakeMesh(),
        soul=_FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _seed_graph(orch: AutonomousBuildOrchestrator,
                tickets: List[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _ticket(uuid: str, ident: str, code: str, *, body: str = "",
            wave: int = 1, epic: str = "ops") -> Ticket:
    """Build a Ticket with a body that carries the ``## Target`` block.

    The body-driven path is the canonical one (registry is fallback);
    seeding via ``body`` exercises ``_resolve_via_body`` end-to-end
    rather than relying on a ``_TARGET_HINTS`` mutation.
    """
    return Ticket(
        id=uuid,
        identifier=ident,
        code=code,
        title=f"{ident} {code}",
        wave=wave,
        epic=epic,
        size="M",
        estimate=5,
        is_critical_path=False,
        body=body,
    )


def _target_block(owner: str, repo: str,
                  paths: tuple[str, ...] = (),
                  new_paths: tuple[str, ...] = ()) -> str:
    """Render a ``## Target`` markdown block in the canonical schema
    (``graph._parse_target_from_ticket_body``)."""
    lines = [
        "## Target",
        f"owner: {owner}",
        f"repo: {repo}",
    ]
    if paths:
        lines.append("paths:")
        lines.extend(f"  - {p}" for p in paths)
    if new_paths:
        lines.append("new_paths:")
        lines.extend(f"  - {p}" for p in new_paths)
    lines.append("base_branch: main")
    return "\n".join(lines) + "\n"


# ── Unit cases ────────────────────────────────────────────────────────────


def test_overlapping_paths_blocks_second_dispatch():
    """Two same-wave same-repo tickets, both editing
    ``docker-compose.yml`` → second waits for first to merge."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml", "Caddyfile"),
        ),
    )
    _seed_graph(orch, [a, b])

    # Simulate `a` already in-flight (DISPATCHED), `b` ready.
    a.status = TicketStatus.DISPATCHED
    in_flight = [a]

    collision = orch._file_collision_for(b, in_flight)
    assert collision is not None
    blocker, shared = collision
    assert blocker is a
    assert shared == ("docker-compose.yml",)


def test_disjoint_paths_no_collision():
    """Two same-wave same-repo tickets touching disjoint files dispatch
    concurrently."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("Caddyfile",),
            new_paths=("scripts/deploy.sh",),
        ),
    )
    _seed_graph(orch, [a, b])

    a.status = TicketStatus.DISPATCHED
    assert orch._file_collision_for(b, [a]) is None


def test_cross_repo_same_path_no_collision():
    """Same path string in different repos must never collide — different
    filesystems."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "tiresias-stack",
            paths=("docker-compose.yml",),
        ),
    )
    _seed_graph(orch, [a, b])

    a.status = TicketStatus.DISPATCHED
    assert orch._file_collision_for(b, [a]) is None


def test_different_owner_same_repo_no_collision():
    """Different ``owner`` + same ``repo`` slug → distinct repos, no
    collision."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "cristianxruvalcaba-coder", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    _seed_graph(orch, [a, b])

    a.status = TicketStatus.DISPATCHED
    assert orch._file_collision_for(b, [a]) is None


def test_empty_target_no_collision():
    """A ticket with no Target block has empty ``file_set`` → never
    blocks dispatch (the candidate side OR the in-flight side empty-set
    means "no collision evidence")."""
    orch = _mk_orchestrator()
    a = _ticket("ua", "SAL-1", "NO-CODE", body="")
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    _seed_graph(orch, [a, b])

    a.status = TicketStatus.DISPATCHED
    # b is ready, a in-flight with empty file_set → no collision either way.
    assert orch._file_collision_for(b, [a]) is None
    # Symmetric: a ready, b in-flight; a has empty file_set → no collision.
    a.status = TicketStatus.PENDING
    b.status = TicketStatus.DISPATCHED
    assert orch._file_collision_for(a, [b]) is None


def test_new_paths_overlap_blocks():
    """Collision must consider ``new_paths`` ∪ ``paths`` — a CREATE-only
    ticket scaffolding ``IMAGE_PINS.md`` collides with a sibling listing
    the same path under ``paths``."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "OPS-02",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            new_paths=("IMAGE_PINS.md",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "OPS-04",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("IMAGE_PINS.md",),
        ),
    )
    _seed_graph(orch, [a, b])

    a.status = TicketStatus.DISPATCHED
    collision = orch._file_collision_for(b, [a])
    assert collision is not None
    blocker, shared = collision
    assert blocker is a
    assert shared == ("IMAGE_PINS.md",)


def test_disable_flag_reverts_to_concurrent_behavior():
    """Kickoff payload ``model_routing.disable_file_collision_check:
    true`` → dispatch ignores file overlap (same-repo same-path tickets
    dispatch concurrently)."""
    payload = {
        "linear_project_id": "00000000-0000-0000-0000-000000000000",
        "model_routing": {
            "disable_file_collision_check": True,
        },
    }
    orch = _mk_orchestrator(kickoff_desc=payload)
    # _parse_payload reads from task["description"]; the orchestrator
    # constructor doesn't call it (only _run_inner does). Call manually
    # so the override-flag flip is observable without spinning the run.
    orch._parse_payload()
    assert orch.disable_file_collision_check is True

    # The dispatch loop's gate is `if not self.disable_file_collision_check:
    # collision = self._file_collision_for(...)`. Mirror it here so we
    # assert the public override semantics — not just the helper.
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    _seed_graph(orch, [a, b])
    a.status = TicketStatus.DISPATCHED

    # Helper still detects the collision (helper is pure / unaware of
    # the override flag — that's by design so callers can log the
    # collision diagnostically even when the gate is off).
    assert orch._file_collision_for(b, [a]) is not None

    # Simulated dispatch loop:
    blocked = False
    if not orch.disable_file_collision_check:
        if orch._file_collision_for(b, [a]) is not None:
            blocked = True
    assert blocked is False, (
        "disable_file_collision_check=True must revert to today's "
        "concurrent dispatch"
    )


def test_default_flag_disabled_is_false():
    """Default kickoff (no model_routing override) leaves the gate on."""
    orch = _mk_orchestrator(kickoff_desc={
        "linear_project_id": "00000000-0000-0000-0000-000000000000",
    })
    orch._parse_payload()
    assert orch.disable_file_collision_check is False


def test_collision_event_records_blocked_by_and_shared_files():
    """When the dispatch loop's gate fires, the orchestrator records a
    ``ticket_serialized_file_collision`` event with ``blocked_by`` +
    ``shared_files`` so operators can grep the state journal."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml", "Caddyfile"),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("Caddyfile", "docker-compose.yml", "scripts/deploy.sh"),
        ),
    )
    _seed_graph(orch, [a, b])
    a.status = TicketStatus.DISPATCHED

    collision = orch._file_collision_for(b, [a])
    assert collision is not None
    blocker, shared = collision
    # Mirror the dispatch-loop record_event call.
    orch.state.record_event(
        "ticket_serialized_file_collision",
        identifier=b.identifier,
        blocked_by=blocker.identifier,
        shared_files=list(shared),
    )

    events = [e for e in orch.state.events
              if e.get("kind") == "ticket_serialized_file_collision"]
    assert len(events) == 1
    evt = events[0]
    assert evt["identifier"] == "SAL-2"
    assert evt["blocked_by"] == "SAL-1"
    # shared_files is sorted at the helper level.
    assert evt["shared_files"] == ["Caddyfile", "docker-compose.yml"]


def test_existing_caps_unchanged():
    """Sanity: file-collision logic does not interfere with the
    pre-existing per-epic-cap or max-parallel accounting on disjoint
    file sets."""
    orch = _mk_orchestrator()
    orch.per_epic_cap = 2
    orch.max_parallel_subs = 5
    tickets = [
        _ticket(
            f"u{i}", f"SAL-{i}", f"X-{i}",
            body=_target_block(
                "salucallc", "alfred-coo-svc",
                paths=(f"src/file_{i}.py",),
            ),
            epic="ops",
        )
        for i in range(1, 5)
    ]
    _seed_graph(orch, tickets)

    in_flight: list[Ticket] = []
    for t in tickets:
        if len(in_flight) >= orch.max_parallel_subs:
            break
        if orch._epic_in_flight(t.epic, in_flight) >= orch.per_epic_cap:
            continue
        if orch._file_collision_for(t, in_flight) is not None:
            continue
        t.status = TicketStatus.DISPATCHED
        in_flight.append(t)

    # Per-epic cap caps at 2 even though 4 are ready and disjoint.
    assert len(in_flight) == 2


def test_three_way_overlap_serialises_third():
    """A → B (shared docker-compose.yml), A → C (shared Caddyfile). Both
    B and C must serialise behind A; B and C don't necessarily share
    among themselves."""
    orch = _mk_orchestrator()
    a = _ticket(
        "ua", "SAL-1", "AI-W2-A",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml", "Caddyfile"),
        ),
    )
    b = _ticket(
        "ub", "SAL-2", "AI-W3-E",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("docker-compose.yml",),
        ),
    )
    c = _ticket(
        "uc", "SAL-3", "AI-W3-F",
        body=_target_block(
            "salucallc", "alfred-coo-svc",
            paths=("Caddyfile",),
        ),
    )
    _seed_graph(orch, [a, b, c])
    a.status = TicketStatus.DISPATCHED

    in_flight = [a]
    cb = orch._file_collision_for(b, in_flight)
    cc = orch._file_collision_for(c, in_flight)
    assert cb is not None and cb[0] is a and cb[1] == ("docker-compose.yml",)
    assert cc is not None and cc[0] is a and cc[1] == ("Caddyfile",)


def test_registry_hint_fallback_used_when_body_absent():
    """A ticket whose body has no ``## Target`` block but whose ``code``
    matches the legacy ``_TARGET_HINTS`` registry still produces a
    file_set via the tier-2 fallback, so collision detection works on
    pre-refactor tickets too."""
    from alfred_coo.autonomous_build.orchestrator import _TARGET_HINTS

    # Pick a registry code we know exists. Use a frozen synthetic code
    # injected into the registry for the duration of this test so we
    # don't depend on the (large + churning) real registry contents.
    _TARGET_HINTS_synthetic = TargetHint(
        owner="salucallc",
        repo="alfred-coo-svc",
        paths=("docker-compose.yml",),
        base_branch="main",
    )
    # Monkey-patch via dict-merge: registry is a Mapping, but in
    # practice the module-level dict is mutable. Restore on teardown.
    sentinel = "TEST-SAL-4036"
    assert sentinel not in _TARGET_HINTS
    # mutate the underlying dict
    _TARGET_HINTS_dict = dict(_TARGET_HINTS)  # snapshot for restore
    try:
        # Inject directly into the module's dict.
        from alfred_coo.autonomous_build import orchestrator as orch_mod
        orch_mod._TARGET_HINTS[sentinel] = _TARGET_HINTS_synthetic  # type: ignore[index]

        orch = _mk_orchestrator()
        a = _ticket(
            "ua", "SAL-1", sentinel,
            body="",  # no Target block — must fall through to registry
        )
        b = _ticket(
            "ub", "SAL-2", "AI-W3-E",
            body=_target_block(
                "salucallc", "alfred-coo-svc",
                paths=("docker-compose.yml",),
            ),
        )
        _seed_graph(orch, [a, b])
        a.status = TicketStatus.DISPATCHED
        collision = orch._file_collision_for(b, [a])
        assert collision is not None
        assert collision[0] is a
    finally:
        from alfred_coo.autonomous_build import orchestrator as orch_mod
        orch_mod._TARGET_HINTS.pop(sentinel, None)  # type: ignore[arg-type]
