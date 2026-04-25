"""SAL-2869 - Destructive-PR guardrail tests.

Three layers exercised here:

- Helper unit tests against fixtures derived from real 2026-04-25
  destructive PRs (#84, #27, #21) and clean refactors (#25, #66).
- Layer 1 prompt assertion: alfred-coo-a + hawkman-qa-a system prompts
  include the DELETION GUARDRAIL substring.
- Layer 2 verdict override: a synthetic APPROVE on a destructive PR
  flips to REQUEST_CHANGES, the merge does NOT fire, the override is
  recorded as a state event.
- Layer 3 pre-merge static check: a hawkman-blind APPROVE that reaches
  _merge_pr with a destructive diff is REFUSED, the ticket
  transitions to FAILED, Linear lands at Backlog, and
  github_merge_pr is never called.
"""

from __future__ import annotations

from typing import Any, Dict, List

from alfred_coo.autonomous_build.destructive_guardrail import (
    GuardrailResult,
    compute_destructive_guardrails,
)
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)
from alfred_coo.persona import BUILTIN_PERSONAS


# Helpers.


def _f(filename: str, additions: int, deletions: int, status: str = "modified") -> Dict[str, Any]:
    """Build a fake GitHub /pulls/N/files entry."""
    return {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
    }


# Helper unit tests, real-PR fixtures.


def test_pr_84_per_file_gate_trips_on_full_file_nuke():
    """PR #84: docker-compose.yml +53/-204 in a 204-LOC file.

    The single-file deletion is 204 = 100% of original_loc, well above
    0.7 * 204 = 143. Per-file gate must trip. The hint description
    contains no rewrite/replace keyword.
    """
    pr_files = [_f("deploy/appliance/docker-compose.yml", 53, 204)]
    result = compute_destructive_guardrails(
        pr_files,
        hint_description="add otel collector config and README",
        original_loc_lookup=lambda p: 204,
    )
    assert result.tripped is True
    assert result.layer == "per_file"
    assert "deploy/appliance/docker-compose.yml" in result.reason
    assert any(
        "deploy/appliance/docker-compose.yml" in c
        for c in result.citations
    )


def test_pr_27_per_pr_ratio_gate_trips_on_mass_delete():
    """PR #27: +46/-2187 across multiple files (router rewrite).

    Total deletions 2187 > 2 * 46 = 92, AND total deletions > 100.
    Per-PR gate trips. To isolate from per-file, this fixture spreads
    the deletions across many small files (each under the per-file
    absolute cap of 500 LOC).
    """
    pr_files = [
        _f(f"src/router_{i}.py", 2, 100) for i in range(22)
    ]  # 22 files, +44/-2200, no individual file > 500 deletions.
    result = compute_destructive_guardrails(
        pr_files,
        hint_description="refactor routers",  # no license keyword
        has_refactor_label=False,
        original_loc_lookup=lambda p: 1000,  # threshold = 500, dels=100 ok
    )
    assert result.tripped is True
    assert result.layer == "per_pr"
    assert "2200" in result.reason


def test_pr_25_clean_consolidation_does_not_trip():
    """PR #25 SS-03: +91/-24, ratio 0.79 - legitimate consolidation.

    24 deletions, never close to 0.7 * any-reasonable-original. Per-PR
    ratio is well below 2x. Must NOT trip.
    """
    pr_files = [_f("src/cot_capture.py", 91, 24)]
    result = compute_destructive_guardrails(
        pr_files,
        hint_description=(
            "rewrite cot_capture.py so /v1/cot/capture writes to _memories"
        ),
        original_loc_lookup=lambda p: 200,
    )
    assert result.tripped is False


def test_pr_66_true_refactor_does_not_trip():
    """PR #66: +60/-58 - true 50/50 refactor. Ratio ~ 0.97."""
    pr_files = [_f("src/foo.py", 60, 58)]
    result = compute_destructive_guardrails(
        pr_files,
        original_loc_lookup=lambda p: 200,
    )
    assert result.tripped is False


def test_refactor_label_excuses_per_pr_gate():
    """PR with -1000/+500 carrying the refactor label must NOT trip.

    The refactor label is the operator's explicit license to do
    wholesale deletions. Per-PR gate disarmed. Per-file gate must
    also be disarmed for this to work; the test passes a large
    original_loc so the per-file threshold is the absolute cap (500).
    Each individual file deletes <=500 here, so per-file does not
    trip; per-PR gate respects the label.
    """
    pr_files = [
        _f("src/a.py", 200, 500),
        _f("src/b.py", 300, 500),
    ]
    result = compute_destructive_guardrails(
        pr_files,
        has_refactor_label=True,
        original_loc_lookup=lambda p: 800,  # threshold=min(560,500)=500
    )
    assert result.tripped is False, (
        f"refactor-labelled PR should not trip; got {result.reason}"
    )


def test_hint_keyword_disarms_per_file_gate():
    """When the hint says `rewrite cot_capture.py`, a 100% deletion
    of cot_capture.py must NOT trip the per-file gate.

    Add enough additions to also clear the per-PR ratio gate so we
    isolate the per-file disarm.
    """
    pr_files = [_f("src/cot_capture.py", 150, 200)]
    result = compute_destructive_guardrails(
        pr_files,
        hint_description="rewrite cot_capture.py to use _memories table",
        original_loc_lookup=lambda p: 200,
    )
    assert result.tripped is False


def test_per_file_threshold_clamps_at_absolute_cap():
    """A 10000-LOC file: 0.7 * 10000 = 7000, but absolute cap is 500.

    A deletion of 600 trips because 600 > min(7000, 500) = 500.
    """
    pr_files = [_f("src/giant.py", 100, 600)]
    result = compute_destructive_guardrails(
        pr_files,
        original_loc_lookup=lambda p: 10000,
    )
    assert result.tripped is True
    assert result.layer == "per_file"


def test_brand_new_file_with_zero_original_loc_does_not_trip_per_file():
    """A brand-new file (lookup returns None): deletions=0 on a
    created file. Confirm no spurious trip."""
    pr_files = [_f("plans/v1-ga/SAL-9999.md", 100, 0, status="added")]
    result = compute_destructive_guardrails(
        pr_files,
        original_loc_lookup=lambda p: None,
    )
    assert result.tripped is False


def test_per_pr_ratio_floor_excuses_small_diffs():
    """PR -50/+10: ratio is 5x but absolute deletions <= 100. Must NOT
    trip the per-PR gate."""
    pr_files = [_f("src/tiny.py", 10, 50)]
    result = compute_destructive_guardrails(
        pr_files,
        original_loc_lookup=lambda p: 200,
    )
    assert result.tripped is False


def test_empty_pr_files_does_not_trip():
    result = compute_destructive_guardrails([])
    assert result.tripped is False


def test_non_list_input_returns_safe_default():
    result = compute_destructive_guardrails(None)  # type: ignore[arg-type]
    assert result.tripped is False


# Layer 1, prompt assertions.


def test_alfred_coo_a_prompt_contains_deletion_guardrail():
    """Layer 1: builder system prompt must carry the DELETION GUARDRAIL
    clause. Asserts the substring so prompt drift is caught at CI."""
    p = BUILTIN_PERSONAS["alfred-coo-a"]
    assert "DELETION GUARDRAIL" in p.system_prompt


def test_hawkman_qa_a_prompt_contains_destructive_guardrail_gate():
    """Layer 2 reinforcement: hawkman's prompt explicitly names the
    destructive-PR guardrail as gate 2.5 so the verifier can catch
    the violation before the orchestrator's programmatic override
    has to fire."""
    p = BUILTIN_PERSONAS["hawkman-qa-a"]
    # Lowercased compare so the case of "destructive-PR" doesn't matter.
    assert "destructive-pr guardrail" in p.system_prompt.lower()


# Layer 2, verdict override.


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
        "id": "kick-2869",
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
    uuid: str = "uG1",
    identifier: str = "SAL-9001",
    code: str = "OPS-01",
    pr_url: str = "https://github.com/salucallc/alfred-coo-svc/pull/84",
    review_task_id: str = "review-2869",
    review_cycles: int = 0,
) -> Ticket:
    t = Ticket(
        id=uuid,
        identifier=identifier,
        code=code,
        title=f"{identifier} {code}",
        wave=1,
        epic="ops",
        size="S",
        estimate=1,
        is_critical_path=False,
    )
    t.status = TicketStatus.REVIEWING
    t.pr_url = pr_url
    t.review_task_id = review_task_id
    t.review_cycles = review_cycles
    t.child_task_id = "child-2869"
    return t


def _approve_review_record(review_task_id: str) -> Dict[str, Any]:
    return {
        "id": review_task_id,
        "status": "completed",
        "result": {
            "tool_calls": [
                {"name": "pr_review", "result": {"state": "APPROVE"}}
            ],
            "summary": "looks good",
        },
    }


# Synthetic destructive PR: per-PR ratio shape, big total deletions
# spread across small files so per-file gate doesn't trip first.
_DESTRUCTIVE_PR_FILES = [
    _f(f"src/router_{i}.py", 2, 100) for i in range(22)
]


async def test_layer_2_verdict_override_flips_approve_to_request_changes(
    monkeypatch,
):
    """Layer 2: hawkman APPROVEs a destructive PR; orchestrator
    overrides to REQUEST_CHANGES. The merge does NOT fire."""
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-2869": _approve_review_record("review-2869"),
    }

    async def _fake_fetch(t):
        return _DESTRUCTIVE_PR_FILES

    monkeypatch.setattr(
        orch, "_fetch_pr_files_for_guardrail", _fake_fetch
    )
    from alfred_coo.autonomous_build import destructive_guardrail as dg

    monkeypatch.setattr(
        dg, "_fetch_original_file_loc", lambda *_a, **_k: 1000
    )

    merge_calls: List[str] = []

    async def _fake_merge(t):
        merge_calls.append(t.identifier)
        return True

    monkeypatch.setattr(orch, "_merge_pr", _fake_merge)

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    await orch._poll_reviews()

    assert merge_calls == [], (
        f"merge should be blocked by Layer 2 override; got {merge_calls}"
    )
    assert orch.state.review_verdicts[ticket.id] == "REQUEST_CHANGES"
    assert ticket.status == TicketStatus.DISPATCHED
    events = [e["kind"] for e in orch.state.events]
    assert "verdict_overridden_destructive" in events
    assert ticket.review_cycles == 1


# Layer 3, pre-merge static check.


async def test_layer_3_pre_merge_static_check_refuses_destructive_pr(
    monkeypatch,
):
    """Layer 3: even if the verdict slips through, _merge_pr runs the
    same guardrail and refuses."""
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])

    async def _fake_fetch(t):
        return _DESTRUCTIVE_PR_FILES

    monkeypatch.setattr(
        orch, "_fetch_pr_files_for_guardrail", _fake_fetch
    )
    from alfred_coo.autonomous_build import destructive_guardrail as dg

    monkeypatch.setattr(
        dg, "_fetch_original_file_loc", lambda *_a, **_k: 1000
    )

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    async def _noop_comment(t, g):
        return None

    monkeypatch.setattr(
        orch, "_post_destructive_guardrail_linear_comment", _noop_comment
    )

    merge_calls: List[Dict[str, Any]] = []
    from alfred_coo import tools as _tools

    real_spec = _tools.BUILTIN_TOOLS.get("github_merge_pr")

    async def _spy_handler(**kwargs):
        merge_calls.append(kwargs)
        return {"ok": True, "sha": "deadbeef"}

    if real_spec is not None:
        monkeypatch.setattr(real_spec, "handler", _spy_handler)

    merged = await orch._merge_pr(ticket)

    assert merged is False, (
        "destructive PR merge must be REFUSED by Layer 3"
    )
    assert merge_calls == [], (
        f"github_merge_pr must NEVER be called; got {merge_calls}"
    )
    events = [e["kind"] for e in orch.state.events]
    assert "merge_blocked_destructive" in events
    assert (ticket.identifier, "Backlog") in linear_calls


async def test_layer_3_static_check_via_handle_review_verdict_marks_failed(
    monkeypatch,
):
    """End-to-end Layer 3: drive _handle_review_verdict with an APPROVE
    that the Layer 2 override happens to miss (we monkeypatch the
    Layer 2 fetch to return None so the override is fail-open), confirm
    _merge_pr returns False AND the caller transitions the ticket to
    FAILED."""
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-2869": _approve_review_record("review-2869"),
    }

    call_log: List[str] = []

    async def _selective_fetch(t):
        # First call comes from Layer 2 (in _handle_review_verdict).
        # Second call comes from Layer 3 (in _merge_pr).
        call_log.append("called")
        if len(call_log) == 1:
            return None  # disarm Layer 2 (fail-open on None)
        return _DESTRUCTIVE_PR_FILES  # arm Layer 3

    monkeypatch.setattr(
        orch, "_fetch_pr_files_for_guardrail", _selective_fetch
    )
    from alfred_coo.autonomous_build import destructive_guardrail as dg

    monkeypatch.setattr(
        dg, "_fetch_original_file_loc", lambda *_a, **_k: 1000
    )

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    async def _noop_comment(t, g):
        return None

    monkeypatch.setattr(
        orch, "_post_destructive_guardrail_linear_comment", _noop_comment
    )

    merge_calls: List[Dict[str, Any]] = []
    from alfred_coo import tools as _tools

    real_spec = _tools.BUILTIN_TOOLS.get("github_merge_pr")

    async def _spy_handler(**kwargs):
        merge_calls.append(kwargs)
        return {"ok": True, "sha": "deadbeef"}

    if real_spec is not None:
        monkeypatch.setattr(real_spec, "handler", _spy_handler)

    await orch._poll_reviews()

    assert ticket.status == TicketStatus.FAILED
    assert merge_calls == [], (
        "github_merge_pr must NEVER be called when Layer 3 trips"
    )
    assert (ticket.identifier, "Backlog") in linear_calls
    events = [e["kind"] for e in orch.state.events]
    assert "merge_blocked_destructive" in events
    assert "ticket_merge_failed" in events
