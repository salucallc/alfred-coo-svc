"""Behavioral APE/V — Hawkman QA hard-gate tests (2026-04-29).

Three layers exercised here, mirroring the SAL-2869 destructive-guardrail
test suite:

- Helper unit tests: 3 PASS scenarios + 3 REQUEST_CHANGES scenarios
  driven directly through ``compute_behavioral_apev``.
- Layer 1 prompt assertion: hawkman-qa-a system prompt names Gate 5 +
  the three reason strings (so prompt drift is caught at CI).
- Layer 2 verdict override: a synthetic APPROVE on a plan-only PR
  flips to REQUEST_CHANGES, the merge does NOT fire, the override is
  recorded as ``verdict_overridden_behavioral_apev``.
- Layer 3 pre-merge static check: a hawkman-blind APPROVE that reaches
  ``_merge_pr`` with a plan-only diff is REFUSED, the ticket
  transitions to FAILED, Linear lands at Backlog, github_merge_pr is
  never called.

The orchestrator wiring mirrors destructive_guardrail; tests share
the same fixture shape for a one-page diff.
"""

from __future__ import annotations

from typing import Any, Dict, List

from alfred_coo.autonomous_build.behavioral_apev import (
    BehavioralGuardrailResult,
    compute_behavioral_apev,
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


def _f(
    filename: str,
    additions: int,
    deletions: int,
    *,
    status: str = "modified",
    patch: str = "",
) -> Dict[str, Any]:
    """Build a fake GitHub /pulls/N/files entry."""
    return {
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": patch,
    }


# ── Helper unit tests ──────────────────────────────────────────────────
# 6 cases: 3 should APPROVE (pass = non-tripped), 3 should REQUEST_CHANGES
# (tripped). Each REQUEST_CHANGES case must trip with the documented
# reason string so the orchestrator override message is stable.


# PASS 1: real code + tests + e2e test for new FastAPI route.
def test_pass_real_code_plus_tests_plus_e2e_route():
    """A PR that adds a new FastAPI route, an implementation file, and a
    test that imports the same module must PASS all three gates."""
    pr_files = [
        _f(
            "src/alfred_coo/foo_routes.py",
            additions=20,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,20 @@\n"
                "+from fastapi import APIRouter\n"
                "+\n"
                "+router = APIRouter()\n"
                "+\n"
                "+@router.get(\"/foo\")\n"
                "+async def get_foo():\n"
                "+    return {\"ok\": True}\n"
            ),
        ),
        _f(
            "tests/test_foo_routes.py",
            additions=15,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,15 @@\n"
                "+from alfred_coo.foo_routes import router\n"
                "+\n"
                "+def test_foo_route_returns_ok(client):\n"
                "+    resp = client.get(\"/foo\")\n"
                "+    assert resp.json() == {\"ok\": True}\n"
            ),
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is False, (
        f"expected APPROVE but tripped: {result.layer} | {result.reason}"
    )


# PASS 2: doc-heavy PR with substantial code AND a test.
def test_pass_doc_heavy_but_real_code_with_test():
    """A doc-heavy PR is fine if it ships real code + a test. Even a
    1000-line plan doc is OK if there's >= 10% non-doc churn AND a
    test."""
    pr_files = [
        _f("plans/v1-ga/MEGA-PLAN.md", additions=1000, deletions=0, status="added"),
        _f(
            "src/alfred_coo/mega.py",
            additions=120,
            deletions=10,
            status="added",
            patch="@@ -0,0 +1,120 @@\n+def mega_helper(x):\n+    return x * 2\n",
        ),
        _f(
            "tests/test_mega.py",
            additions=30,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,30 @@\n"
                "+from alfred_coo.mega import mega_helper\n"
                "+\n"
                "+def test_mega_doubles():\n"
                "+    assert mega_helper(3) == 6\n"
            ),
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is False, (
        f"expected APPROVE but tripped: {result.layer} | {result.reason}"
    )


# PASS 3: small bug-fix to existing source plus a modified test that
# references the changed module — the most common legitimate PR shape.
def test_pass_small_bugfix_with_modified_test():
    """A PR that modifies a single source file (small bug fix) plus
    updates an existing test file that imports the changed module
    must PASS all three gates. No new public surface, so B3 is
    vacuous; B1 ratio is 100% non-doc; B2 finds the module reference
    in the modified test diff."""
    pr_files = [
        _f(
            "src/alfred_coo/persona.py",
            additions=3,
            deletions=2,
            patch=(
                "@@ -100,2 +100,3 @@\n"
                "-    return None\n"
                "+    if not value:\n"
                "+        return None\n"
                "+    return value.strip()\n"
            ),
        ),
        _f(
            "tests/test_persona.py",
            additions=5,
            deletions=1,
            patch=(
                "@@ -50,1 +50,5 @@\n"
                "-from alfred_coo.persona import BUILTIN_PERSONAS\n"
                "+from alfred_coo.persona import BUILTIN_PERSONAS\n"
                "+\n"
                "+def test_persona_strips_whitespace():\n"
                "+    p = BUILTIN_PERSONAS['hawkman-qa-a']\n"
                "+    assert p.name == p.name.strip()\n"
            ),
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is False, (
        f"expected APPROVE but tripped: {result.layer} | {result.reason}"
    )


# REJECT 1: plan-only PR — single .md file, no code, no tests.
def test_reject_plan_only_no_implementation():
    """A PR that adds a single 200-line plan doc with no code and no
    tests must trip B1 with reason ``plan_only_no_implementation``."""
    pr_files = [
        _f(
            "plans/v1-ga/AB-22-tiresias-integration.md",
            additions=200,
            deletions=0,
            status="added",
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is True
    assert result.layer == "plan_only_no_implementation"
    assert "plan-only" in result.reason.lower()


# REJECT 2: tests don't cover the changed sources.
def test_reject_tests_dont_cover_changes():
    """A PR that ships real source changes plus a modified test, but the
    test diff doesn't reference any changed-source symbol or module,
    must trip B2 with reason ``tests_dont_cover_changes``."""
    pr_files = [
        _f(
            "src/alfred_coo/persona.py",
            additions=40,
            deletions=5,
            patch=(
                "@@ -100,0 +101,40 @@\n"
                "+def new_persona_helper():\n"
                "+    return 'hello'\n"
            ),
        ),
        # Modified test that talks about something else (an unrelated
        # module). It does NOT mention persona.py or new_persona_helper.
        _f(
            "tests/test_unrelated.py",
            additions=10,
            deletions=2,
            patch=(
                "@@ -50,0 +51,10 @@\n"
                "+from alfred_coo.tools import BUILTIN_TOOLS\n"
                "+\n"
                "+def test_tools_dict_exists():\n"
                "+    assert BUILTIN_TOOLS is not None\n"
            ),
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is True
    assert result.layer == "tests_dont_cover_changes"


# REJECT 3: new public surface (FastAPI route) with no e2e test.
def test_reject_surface_change_lacks_e2e_test():
    """A PR that adds a new FastAPI route but ships only a test of a
    different module must trip B3 with reason
    ``surface_change_lacks_e2e_test``.

    Pre-condition: B1 and B2 must NOT trip first (surface PR with a
    test that touches *some* changed source — but not the route's
    module). We accomplish this by:
      - The route file (src/alfred_coo/new_routes.py) IS in the diff.
      - A separate non-route source file is also changed and IS
        referenced by the test (so B2 passes).
      - The route module is NOT referenced by any test (so B3 trips).
    """
    pr_files = [
        _f(
            "src/alfred_coo/new_routes.py",
            additions=15,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,15 @@\n"
                "+from fastapi import APIRouter\n"
                "+\n"
                "+router = APIRouter()\n"
                "+\n"
                "+@router.post(\"/widget\")\n"
                "+async def make_widget():\n"
                "+    return {\"id\": 1}\n"
            ),
        ),
        _f(
            "src/alfred_coo/utility.py",
            additions=10,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,10 @@\n"
                "+def util_one():\n"
                "+    return 1\n"
            ),
        ),
        # Test references utility (so B2 passes) but never imports
        # new_routes (so B3 trips on the new POST surface).
        _f(
            "tests/test_utility.py",
            additions=10,
            deletions=0,
            status="added",
            patch=(
                "@@ -0,0 +1,10 @@\n"
                "+from alfred_coo.utility import util_one\n"
                "+\n"
                "+def test_util_one():\n"
                "+    assert util_one() == 1\n"
            ),
        ),
    ]
    result = compute_behavioral_apev(pr_files)
    assert result.tripped is True
    assert result.layer == "surface_change_lacks_e2e_test"
    assert "new_routes" in "; ".join(result.citations)


# ── Layer 1: prompt assertions ────────────────────────────────────────


def test_hawkman_qa_a_prompt_contains_gate_5_block():
    """The hawkman-qa-a prompt must reference Gate 5 + the three
    behavioral reason strings, so prompt drift away from the new
    behavioral checklist is caught at CI."""
    p = BUILTIN_PERSONAS["hawkman-qa-a"]
    prompt = p.system_prompt
    assert "GATE 5" in prompt
    assert "plan_only_no_implementation" in prompt
    assert "tests_dont_cover_changes" in prompt
    assert "surface_change_lacks_e2e_test" in prompt


# ── Layer 2 + 3: orchestrator wiring ──────────────────────────────────


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
        "id": "kick-behav",
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


def _seed_graph(
    orch: AutonomousBuildOrchestrator, tickets: List[Ticket]
) -> None:
    g = TicketGraph()
    for t in tickets:
        g.nodes[t.id] = t
        g.identifier_index[t.identifier] = t.id
    orch.graph = g


def _mk_reviewing_ticket(
    uuid: str = "uH1",
    identifier: str = "SAL-9101",
    code: str = "AB-22",
    pr_url: str = "https://github.com/salucallc/alfred-coo-svc/pull/501",
    review_task_id: str = "review-behav",
    review_cycles: int = 0,
) -> Ticket:
    t = Ticket(
        id=uuid,
        identifier=identifier,
        code=code,
        title=f"{identifier} {code}",
        wave=1,
        epic="ab",
        size="S",
        estimate=1,
        is_critical_path=False,
    )
    t.status = TicketStatus.REVIEWING
    t.pr_url = pr_url
    t.review_task_id = review_task_id
    t.review_cycles = review_cycles
    t.child_task_id = "child-behav"
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


# Synthetic plan-only PR: a single 200-line .md doc, status=added.
_PLAN_ONLY_PR_FILES = [
    _f(
        "plans/v1-ga/AB-22-plan.md",
        additions=200,
        deletions=0,
        status="added",
    ),
]


async def test_layer_2_verdict_override_flips_plan_only_approve_to_request_changes(
    monkeypatch,
):
    """Layer 2: hawkman APPROVEs a plan-only PR; orchestrator overrides
    to REQUEST_CHANGES. The merge does NOT fire.

    Disarms the destructive-guardrail Layer 2 (returns no-trip) so we
    isolate the behavioral-apev override path. The destructive
    guardrail's _check_destructive_guardrail_for_ticket is patched to
    a non-tripping result, while _check_behavioral_apev_for_ticket
    runs against the real plan-only file list."""
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-behav": _approve_review_record("review-behav"),
    }

    async def _fake_fetch(t):
        return _PLAN_ONLY_PR_FILES

    # Both behavioral-apev and destructive guardrail share this fetch;
    # destructive guardrail won't trip on a single 200-line .md add
    # (no deletions), so the only override that fires is behavioral.
    monkeypatch.setattr(
        orch, "_fetch_pr_files_for_guardrail", _fake_fetch
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
        f"merge should be blocked by Layer 2 behavioral override; "
        f"got {merge_calls}"
    )
    assert orch.state.review_verdicts[ticket.id] == "REQUEST_CHANGES"
    assert ticket.status == TicketStatus.DISPATCHED
    events = [e["kind"] for e in orch.state.events]
    assert "verdict_overridden_behavioral_apev" in events, (
        f"expected verdict_overridden_behavioral_apev event; "
        f"got events: {events}"
    )
    # Confirm the recorded event captured the gate name.
    behav_event = next(
        e for e in orch.state.events
        if e["kind"] == "verdict_overridden_behavioral_apev"
    )
    assert behav_event["layer"] == "plan_only_no_implementation"
    assert ticket.review_cycles == 1


async def test_layer_3_pre_merge_static_check_refuses_plan_only_pr(
    monkeypatch,
):
    """Layer 3: even if the verdict slips through, _merge_pr runs the
    same behavioral-apev gate and refuses."""
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])

    async def _fake_fetch(t):
        return _PLAN_ONLY_PR_FILES

    monkeypatch.setattr(
        orch, "_fetch_pr_files_for_guardrail", _fake_fetch
    )

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    async def _noop_destructive_comment(t, g):
        return None

    monkeypatch.setattr(
        orch,
        "_post_destructive_guardrail_linear_comment",
        _noop_destructive_comment,
    )

    async def _noop_behav_comment(t, b):
        return None

    monkeypatch.setattr(
        orch,
        "_post_behavioral_apev_linear_comment",
        _noop_behav_comment,
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
        "plan-only PR merge must be REFUSED by Layer 3 behavioral check"
    )
    assert merge_calls == [], (
        f"github_merge_pr must NEVER be called; got {merge_calls}"
    )
    events = [e["kind"] for e in orch.state.events]
    assert "merge_blocked_behavioral_apev" in events, (
        f"expected merge_blocked_behavioral_apev event; got events: {events}"
    )
    assert (ticket.identifier, "Backlog") in linear_calls
