"""SAL-4101 (substrate doctor, retry-loop side) tests.

Covers the orchestrator-side TRIAGE_NEEDED coercion: when Hawkman issues
N consecutive REQUEST_CHANGES verdicts on the same PR citing the same
``Gate <token>``, the orchestrator must:

  (a) transition the ticket to ``TicketStatus.TRIAGE_NEEDED``
  (b) NOT re-dispatch the builder (no new mesh task created)
  (c) construct a Slack ``#batcave`` Tier-2 alert payload (mock asserted)

Threshold is env-overridable via ``TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD``
(default 3).

Reuses the fakes from ``test_autonomous_build_review_loop.py`` so the
test rig matches the AB-08 conventions exactly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
    _extract_gate_citations,
    _triage_needed_gate_repeat_threshold,
)


# ── Fakes (mirror test_autonomous_build_review_loop.py exactly) ─────────────


class _FakeMesh:
    def __init__(self) -> None:
        self.created: List[Dict[str, Any]] = []
        self._next_id = 100

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {
            "title": title,
            "description": description,
            "from_session_id": from_session_id,
        }
        self.created.append(rec)
        nid = f"respawn-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        pass


class _FakeSoul:
    def __init__(self) -> None:
        self.writes: List[Dict[str, Any]] = []

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


def _mk_orchestrator(mesh=None, soul=None) -> AutonomousBuildOrchestrator:
    task = {
        "id": "kick-sal4101",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": "",
    }
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


def _seed_graph(orch: AutonomousBuildOrchestrator, tickets: List[Ticket]) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _mk_reviewing_ticket(
    uuid: str = "uA",
    identifier: str = "SAL-3243",
    code: str = "TIR-XX",
    pr_url: str = "https://github.com/salucallc/foo/pull/279",
    review_task_id: str = "review-1",
    review_cycles: int = 0,
) -> Ticket:
    t = Ticket(
        id=uuid,
        identifier=identifier,
        code=code,
        title=f"{identifier} {code}",
        wave=1,
        epic="tiresias",
        size="S",
        estimate=1,
        is_critical_path=False,
    )
    t.status = TicketStatus.REVIEWING
    t.pr_url = pr_url
    t.review_task_id = review_task_id
    t.review_cycles = review_cycles
    t.child_task_id = "child-1"
    return t


def _gate1_review_record(review_task_id: str) -> Dict[str, Any]:
    """Shape a completed review task whose body cites ``Gate 1``."""
    body = (
        "REQUEST_CHANGES: Gate 1 (APE/V citation) is missing — the PR body "
        "does not include the canonical APE/V Acceptance section. Please "
        "copy the section verbatim from the ticket body. Gate 5b also "
        "fails: assertions are placeholder `assert True` stubs."
    )
    return {
        "id": review_task_id,
        "status": "completed",
        "result": {
            "tool_calls": [
                {
                    "name": "pr_review",
                    "result": {
                        "state": "REQUEST_CHANGES",
                        "body": body,
                    },
                }
            ],
            "summary": "REQUEST_CHANGES: " + body,
        },
    }


# ── Acceptance test (Acceptance §4) ─────────────────────────────────────────


async def test_three_consecutive_gate1_skips_coerce_triage_needed(monkeypatch):
    """Acceptance §4: simulate 3 consecutive REQUEST_CHANGES citing Gate 1
    on the same PR. Assert (a) status=TRIAGE_NEEDED, (b) no new dispatch,
    (c) Slack alert payload constructed.
    """
    # Pin the threshold to the documented default so the test is
    # insensitive to env overrides on the runner.
    monkeypatch.setenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", "3")
    assert _triage_needed_gate_repeat_threshold() == 3

    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])

    # Stub Linear update so the test does not require the AB-03 tools.
    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    # Mock the Slack alert sink. cadence.post is the canonical helper used
    # elsewhere in the orchestrator for direct event-driven posts.
    slack_calls: List[str] = []

    async def _fake_slack_post(message: str):
        slack_calls.append(message)
        return {"ok": True}

    monkeypatch.setattr(orch.cadence, "post", _fake_slack_post)

    # Cycle 1 — under threshold, should respawn normally and bump cycle.
    orch._last_completed_by_id = {"review-1": _gate1_review_record("review-1")}
    await orch._poll_reviews()
    assert ticket.status == TicketStatus.DISPATCHED
    assert ticket.review_cycles == 1
    assert len(mesh.created) == 1
    assert len(slack_calls) == 0
    # Re-arm for the next review.
    ticket.status = TicketStatus.REVIEWING
    ticket.review_task_id = "review-2"

    # Cycle 2 — still under threshold.
    orch._last_completed_by_id = {"review-2": _gate1_review_record("review-2")}
    await orch._poll_reviews()
    assert ticket.status == TicketStatus.DISPATCHED
    assert ticket.review_cycles == 2
    assert len(mesh.created) == 2
    assert len(slack_calls) == 0
    # Re-arm one more time.
    ticket.status = TicketStatus.REVIEWING
    ticket.review_task_id = "review-3"

    # Cycle 3 — threshold crossed. Coerce to TRIAGE_NEEDED, NO new mesh
    # task, Slack alert constructed, Linear moved to Triage.
    mesh_count_before = len(mesh.created)
    orch._last_completed_by_id = {"review-3": _gate1_review_record("review-3")}
    await orch._poll_reviews()

    # (a) ticket transitions to TRIAGE_NEEDED
    assert ticket.status == TicketStatus.TRIAGE_NEEDED, (
        f"expected TRIAGE_NEEDED, got {ticket.status}"
    )
    # (b) no further dispatch issued
    assert len(mesh.created) == mesh_count_before, (
        f"expected no new mesh tasks, got {len(mesh.created) - mesh_count_before}"
    )
    # review_cycles MUST NOT bump on coercion (we exited before the bump)
    assert ticket.review_cycles == 2
    # (c) Slack alert payload constructed exactly once with the canonical
    # tokens (PR URL, ticket identifier, gate citation, count).
    assert len(slack_calls) == 1
    payload = slack_calls[0]
    assert "SAL-4101" in payload
    assert "TRIAGE_NEEDED" in payload
    assert ticket.identifier in payload
    assert ticket.pr_url in payload
    assert "gate 1" in payload.lower()
    # Linear was moved to Triage.
    assert (ticket.identifier, "Triage") in linear_calls
    # Event was recorded with the canonical kind.
    events = [e["kind"] for e in orch.state.events]
    assert "ticket_triage_needed" in events
    # Bookkeeping is persisted on state for cross-restart durability.
    key = orch._gate_repeat_state_key(ticket)
    rec = orch.state.consecutive_same_gate_skips.get(key)
    assert rec is not None
    assert rec["gate"] == "gate 1"
    assert rec["count"] == 3


# ── Helper / unit coverage ──────────────────────────────────────────────────


def test_extract_gate_citations_parses_canonical_forms():
    body = (
        "Gate 1 (APE/V) — missing.\n"
        "gate-5a placeholder body.\n"
        "Investigate further; Gate 5b also fails."
    )
    cites = _extract_gate_citations(body)
    assert "gate 1" in cites
    assert "gate 5a" in cites
    assert "gate 5b" in cites
    # The `Investigate` token must NOT match — guarded by the
    # ``(?<![A-Za-z])`` lookbehind.
    assert all(not c.startswith("gate i") for c in cites)


def test_extract_gate_citations_empty_input():
    assert _extract_gate_citations("") == frozenset()
    assert _extract_gate_citations(None) == frozenset()  # type: ignore[arg-type]


def test_threshold_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", raising=False)
    assert _triage_needed_gate_repeat_threshold() == 3


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", "5")
    assert _triage_needed_gate_repeat_threshold() == 5


def test_threshold_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", "garbage")
    assert _triage_needed_gate_repeat_threshold() == 3
    monkeypatch.setenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", "0")
    assert _triage_needed_gate_repeat_threshold() == 3
    monkeypatch.setenv("TRIAGE_NEEDED_GATE_REPEAT_THRESHOLD", "-1")
    assert _triage_needed_gate_repeat_threshold() == 3


def test_triage_needed_is_terminal_state():
    """Wave loop must stop polling once a ticket lands in TRIAGE_NEEDED."""
    from alfred_coo.autonomous_build.graph import TERMINAL_STATES

    assert TicketStatus.TRIAGE_NEEDED in TERMINAL_STATES
