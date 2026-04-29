"""silent_with_tools recovery tests (2026-04-29).

Coverage for the silent-complete fallback chain added to
``_poll_children``: a builder that completes its tool-loop without
calling ``propose_pr`` (gpt-oss:120b-cloud silent_with_tools, observed
~16% baseline / 50%+ under load) is now re-dispatched with the next
model in ``builder.fallback_chain`` rather than being marked FAILED on
the first empty envelope. Hard cap at 4 attempts (primary + 3
fallbacks); past the cap the ticket lands in terminal FAILED with
``last_failure_reason = silent_with_tools_chain_exhausted``.
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


# ── Fakes (kept independent of the flat suite) ────────────────────────────


class _FakeMesh:
    def __init__(self, completed_tasks=None):
        self.created: list[dict] = []
        self.completed_tasks = list(completed_tasks or [])
        self._next_id = 1

    async def create_task(self, *, title, description="", from_session_id=None):
        rec = {"title": title, "description": description,
               "from_session_id": from_session_id}
        self.created.append(rec)
        nid = f"child-{self._next_id}"
        self._next_id += 1
        return {"id": nid, "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        if status:
            return [t for t in self.completed_tasks
                    if (t.get("status") or "").lower() == status.lower()]
        return list(self.completed_tasks)


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
        "id": "kick-silent",
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


# ── Predicate: _envelope_is_silent_complete ───────────────────────────────


def test_envelope_is_silent_complete_true_for_empty_completion():
    """Empty-summary envelope with no follow-up + no artifacts is the
    canonical silent-with-tools shape: model finished its tool-use loop
    without calling propose_pr and returned an empty envelope.
    """
    envelope = {
        "summary": "",
        "follow_up_tasks": [],
        "tool_calls": [],
    }
    assert AutonomousBuildOrchestrator._envelope_is_silent_complete(envelope)


def test_envelope_is_silent_complete_false_when_pr_url_present():
    """A non-empty summary referencing the PR URL is a happy-path emit;
    the predicate must return False so the silent-with-tools redispatch
    branch never fires for a healthy completion. (The PR-URL extraction
    happens upstream of this predicate at the ``_extract_pr_url`` call,
    so this assertion mirrors the ``has_pr_url=False`` precondition by
    asserting the predicate alone rejects an envelope with a non-empty
    summary.)
    """
    envelope = {
        "summary": "Opened PR https://github.com/foo/bar/pull/123 with the fix.",
        "follow_up_tasks": [],
        "tool_calls": [],
    }
    assert not AutonomousBuildOrchestrator._envelope_is_silent_complete(envelope)


def test_envelope_is_silent_complete_false_when_grounding_gap_artifacts_present():
    """An envelope with a populated artifacts list (e.g. from a
    grounding-gap escalate emit) is actionable, NOT silent. The
    grounding-gap classifier runs upstream and routes the ticket to
    ESCALATED before the silent-complete predicate is consulted; this
    test pins the predicate's own behaviour so a future refactor can't
    silently mis-route a grounding-gap envelope through the
    silent-with-tools branch.
    """
    envelope = {
        "summary": "",
        "artifacts": [{"path": "grounding-gap-notes.md", "content": "..."}],
    }
    assert not AutonomousBuildOrchestrator._envelope_is_silent_complete(envelope)


def test_envelope_is_silent_complete_false_when_long_summary_present():
    """A long, non-empty summary is by definition NOT a silent
    completion — the model produced output, even if it didn't open a
    PR. (The no_pr_url failure_reason path catches this case downstream.)
    """
    envelope = {
        "summary": (
            "I considered the task carefully and analysed the affected "
            "files but determined that the change should be split across "
            "two PRs to keep the review surface manageable. Here is my "
            "rationale: ..."
        ),
        "follow_up_tasks": [],
        "tool_calls": [],
    }
    assert not AutonomousBuildOrchestrator._envelope_is_silent_complete(envelope)


# ── Picker: _pick_next_fallback_model ─────────────────────────────────────


@pytest.fixture
def _no_registry(monkeypatch):
    """Force ``_pick_next_fallback_model`` onto the in-memory
    ``builder_fallback_chain`` path by stubbing both the registry loader
    and the ``_pick_model_for_role`` consultation. Tests that pin a
    specific chain need this so the on-disk
    ``Z:/_planning/model_registry/registry.yaml`` doesn't sneak in.
    """
    import alfred_coo.autonomous_build.model_registry as mr
    monkeypatch.setattr(mr, "_load_model_registry", lambda *a, **kw: None)
    return mr


def test_pick_next_fallback_model_walks_chain_by_attempt_index(_no_registry):
    """``_pick_next_fallback_model`` returns ``chain[attempt]`` (0-indexed)
    for the configured ``builder_fallback_chain``. attempt=0 → first
    chain entry; attempt=1 → second.
    """
    orch = _mk_orch()
    # Pin the in-memory chain so this test doesn't depend on the
    # on-disk model registry yaml (which may exist on the operator's
    # box but not in CI). The ``_no_registry`` fixture stubs the
    # registry loader to None so the picker uses the in-memory chain.
    orch.builder_fallback_chain = (
        "model-zero", "model-one", "model-two", "model-three",
    )

    assert orch._pick_next_fallback_model("builder", attempt=0) == "model-zero"
    assert orch._pick_next_fallback_model("builder", attempt=1) == "model-one"
    assert orch._pick_next_fallback_model("builder", attempt=2) == "model-two"
    assert orch._pick_next_fallback_model("builder", attempt=3) == "model-three"


def test_pick_next_fallback_model_returns_none_past_chain_end(_no_registry):
    """Past the last index of the chain, the picker returns ``None`` so
    the silent_with_tools branch in ``_poll_children`` can detect
    chain-exhausted and route to terminal FAILED.
    """
    orch = _mk_orch()
    orch.builder_fallback_chain = ("model-zero", "model-one")

    # Chain length 2 → indexes 0 + 1 valid; 2+ exhausted.
    assert orch._pick_next_fallback_model("builder", attempt=0) == "model-zero"
    assert orch._pick_next_fallback_model("builder", attempt=1) == "model-one"
    assert orch._pick_next_fallback_model("builder", attempt=2) is None
    assert orch._pick_next_fallback_model("builder", attempt=10) is None


# ── _poll_children integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_children_redispatches_on_silent_complete_under_cap(
    monkeypatch, _no_registry,
):
    """Silent-complete envelope + ``dispatch_attempts < 4`` must trigger
    ``_redispatch_child_with_model`` (called once with the next chain
    model) and leave the ticket in PENDING for the next dispatch tick.
    The ticket must NOT land in FAILED.
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)
    orch.builder_fallback_chain = (
        "primary", "fallback-1", "fallback-2", "fallback-3",
    )

    t = _t("u1", "SAL-2999", code="TIR-06")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-silent-1"
    t.dispatch_attempts = 1  # one prior attempt; next slot is fallback-1
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-silent")

    # Mock the redispatch helper so we can assert it was called with
    # the right args without re-firing a real mesh dispatch.
    redispatch_mock = AsyncMock()
    monkeypatch.setattr(orch, "_redispatch_child_with_model", redispatch_mock)

    async def _fake_update_linear(ticket, state_name):
        return None
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update_linear)

    # Truncated tool-loop envelope = canonical silent_with_tools shape.
    mesh.completed_tasks.append({
        "id": "child-silent-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2999 ...",
        "status": "completed",
        "result": {
            "content": "[tool-use loop exceeded max iterations; partial]",
            "truncated": True,
            "tool_calls": [],
        },
    })

    await orch._poll_children()

    # Redispatch fired exactly once with the expected next model.
    redispatch_mock.assert_awaited_once()
    args, kwargs = redispatch_mock.call_args
    # Helper signature: (ticket, next_model). Check positional or kwarg.
    if args:
        called_ticket, called_model = args[0], args[1]
    else:
        called_ticket = kwargs.get("ticket")
        called_model = kwargs.get("next_model")
    assert called_ticket is t
    assert called_model == "fallback-1"

    # Ticket must NOT be terminal FAILED on a redispatch path.
    assert t.status != TicketStatus.FAILED


@pytest.mark.asyncio
async def test_poll_children_marks_failed_on_chain_exhausted(
    monkeypatch, _no_registry,
):
    """Silent-complete envelope at ``dispatch_attempts >= 4`` is
    terminal: the ticket lands in FAILED with
    ``last_failure_reason = silent_with_tools_chain_exhausted`` and the
    redispatch helper is NOT called. ``retry_budget=0`` pins the
    terminal-FAILED transition (the retry-budget sweep would otherwise
    route through BACKED_OFF on the default budget of 2).
    """
    mesh = _FakeMesh()
    orch = _mk_orch(mesh=mesh)
    orch.builder_fallback_chain = (
        "primary", "fallback-1", "fallback-2", "fallback-3",
    )

    t = _t("u1", "SAL-2999", code="TIR-06")
    t.status = TicketStatus.DISPATCHED
    t.child_task_id = "child-silent-1"
    # Past the 4-attempt hard cap; chain is exhausted.
    t.dispatch_attempts = 4
    t.retry_budget = 0  # pin terminal-FAILED
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-silent")

    redispatch_mock = AsyncMock()
    monkeypatch.setattr(orch, "_redispatch_child_with_model", redispatch_mock)

    linear_calls: list[tuple[str, str]] = []
    async def _fake_update_linear(ticket, state_name):
        linear_calls.append((ticket.identifier, state_name))
    monkeypatch.setattr(orch, "_update_linear_state", _fake_update_linear)

    mesh.completed_tasks.append({
        "id": "child-silent-1",
        "title": "[persona:alfred-coo-a] [wave-0] [tiresias] SAL-2999 ...",
        "status": "completed",
        "result": {
            "content": "[tool-use loop exceeded max iterations; partial]",
            "truncated": True,
            "tool_calls": [],
        },
    })

    await orch._poll_children()

    # Redispatch must NOT have fired — chain is exhausted.
    redispatch_mock.assert_not_awaited()
    # Ticket lands in terminal FAILED with the explicit chain-exhausted
    # tag on ``last_failure_reason``.
    assert t.status == TicketStatus.FAILED, (
        f"expected FAILED on chain-exhausted, got {t.status}"
    )
    assert t.last_failure_reason == "silent_with_tools_chain_exhausted", (
        f"expected silent_with_tools_chain_exhausted reason, got "
        f"{t.last_failure_reason!r}"
    )
    # Linear must roll back to Backlog so the ticket re-enters the
    # ready queue when an operator clears the failure.
    assert ("SAL-2999", "Backlog") in linear_calls
