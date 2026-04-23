"""AB-08 tests: REVIEWING → MERGED_GREEN review completion loop.

Exercises `_poll_reviews`, `_extract_verdict`, `_parse_fallback_verdict`,
`_merge_pr`, `_respawn_child_with_fixes`, and the state round-trip for
`review_task_ids` / `review_verdicts` / `merged_pr_urls`.

Test matrix (8 cases, matches AB-08 design doc §Tests):

1. APPROVE + merge OK                 → MERGED_GREEN + Linear Done
2. APPROVE + merge fails              → FAILED + Linear Backlog
3. REQUEST_CHANGES under cap          → respawned child, cycles=1,
                                        status=DISPATCHED
4. REQUEST_CHANGES at cap (=3)        → FAILED
5. COMMENTED_FALLBACK with APPROVE
   intended_event                     → treated as APPROVE
6. Silent review once                 → `_dispatch_review` re-called,
                                        silent_review_retries=1
7. Merge idempotency                  → _merge_pr on already-merged
                                        ticket returns True without
                                        calling github_merge_pr
8. State round-trip                   → review_task_ids, review_verdicts,
                                        and merged_pr_urls survive
                                        to_json/from_json
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    MAX_REVIEW_CYCLES,
    AutonomousBuildOrchestrator,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes ──────────────────────────────────────────────────────────────────


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
        "id": "kick-ab08",
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
    identifier: str = "SAL-1",
    code: str = "TIR-01",
    pr_url: str = "https://github.com/salucallc/foo/pull/7",
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


def _review_record(
    review_task_id: str,
    *,
    state: Optional[str] = None,
    summary: Optional[str] = None,
    intended_event: Optional[str] = None,
    extra_tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Shape a fake completed mesh-task record carrying a review verdict.

    `state` drives the pr_review tool-call result.state; `intended_event`
    covers the COMMENTED_FALLBACK path; `summary` covers the regex path.
    """
    tool_calls: List[Dict[str, Any]] = []
    if state is not None:
        call: Dict[str, Any] = {
            "name": "pr_review",
            "result": {"state": state},
        }
        if intended_event is not None:
            call["result"]["intended_event"] = intended_event
        tool_calls.append(call)
    if extra_tool_calls:
        tool_calls.extend(extra_tool_calls)
    result: Dict[str, Any] = {"tool_calls": tool_calls}
    if summary is not None:
        result["summary"] = summary
    return {
        "id": review_task_id,
        "status": "completed",
        "result": result,
    }


# ── Test 1: APPROVE + merge OK → MERGED_GREEN + Linear Done ─────────────────


async def test_approve_triggers_merge_and_marks_green(monkeypatch):
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-1": _review_record("review-1", state="APPROVE"),
    }

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    async def _fake_merge(t):
        orch.state.merged_pr_urls[t.id] = "abc123sha"
        return True

    monkeypatch.setattr(orch, "_merge_pr", _fake_merge)

    updated = await orch._poll_reviews()

    assert len(updated) == 1
    assert ticket.status == TicketStatus.MERGED_GREEN
    assert orch.state.merged_pr_urls[ticket.id] == "abc123sha"
    assert orch.state.review_verdicts[ticket.id] == "APPROVE"
    assert (ticket.identifier, "Done") in linear_calls
    events = [e["kind"] for e in orch.state.events]
    assert "ticket_merged" in events


# ── Test 2: APPROVE + merge fails → FAILED + Linear Backlog ─────────────────


async def test_approve_merge_failure_marks_failed(monkeypatch):
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-1": _review_record("review-1", state="APPROVE"),
    }

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    async def _fake_merge(t):
        return False

    monkeypatch.setattr(orch, "_merge_pr", _fake_merge)

    await orch._poll_reviews()

    assert ticket.status == TicketStatus.FAILED
    assert ticket.id not in orch.state.merged_pr_urls
    assert (ticket.identifier, "Backlog") in linear_calls
    events = [e["kind"] for e in orch.state.events]
    assert "ticket_merge_failed" in events
    assert "ticket_merged" not in events


# ── Test 3: REQUEST_CHANGES under cap → respawn + DISPATCHED ────────────────


async def test_request_changes_respawns_child(monkeypatch):
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    ticket = _mk_reviewing_ticket(review_cycles=0)
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-1": _review_record(
            "review-1",
            state="REQUEST_CHANGES",
            summary="REQUEST_CHANGES: fix the null check on line 42",
            extra_tool_calls=[
                {
                    "name": "pr_review",
                    "result": {
                        "state": "REQUEST_CHANGES",
                        "body": "fix the null check on line 42",
                    },
                }
            ],
        ),
    }

    async def _noop_linear(t, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _noop_linear)

    await orch._poll_reviews()

    assert ticket.status == TicketStatus.DISPATCHED
    assert ticket.review_cycles == 1
    assert ticket.silent_review_retries == 0
    # Stale review task id cleared so the next review dispatch seeds fresh.
    assert ticket.review_task_id is None
    assert ticket.id not in orch.state.review_task_ids
    # A new child task was created on the mesh.
    assert len(mesh.created) == 1
    assert "fix: round 1" in mesh.created[0]["title"]
    assert ticket.child_task_id.startswith("respawn-")
    events = [e["kind"] for e in orch.state.events]
    assert "ticket_respawned" in events


# ── Test 4: REQUEST_CHANGES at cap → FAILED ─────────────────────────────────


async def test_max_review_cycles_marks_failed(monkeypatch):
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    ticket = _mk_reviewing_ticket(review_cycles=MAX_REVIEW_CYCLES)
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-1": _review_record("review-1", state="REQUEST_CHANGES"),
    }

    linear_calls: List[tuple] = []

    async def _fake_linear(t, state_name):
        linear_calls.append((t.identifier, state_name))

    monkeypatch.setattr(orch, "_update_linear_state", _fake_linear)

    await orch._poll_reviews()

    assert ticket.status == TicketStatus.FAILED
    # No respawn fired.
    assert len(mesh.created) == 0
    # review_cycles preserved at the cap for diagnostics.
    assert ticket.review_cycles == MAX_REVIEW_CYCLES
    assert (ticket.identifier, "Backlog") in linear_calls
    events = [e["kind"] for e in orch.state.events]
    assert "review_max_cycles" in events


# ── Test 5: COMMENTED_FALLBACK with APPROVE intended_event → APPROVE ────────


async def test_commented_fallback_with_approve_intended_event(monkeypatch):
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    orch._last_completed_by_id = {
        "review-1": _review_record(
            "review-1",
            state="COMMENTED_FALLBACK",
            intended_event="APPROVE",
        ),
    }

    async def _noop_linear(t, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _noop_linear)

    merge_calls: List[str] = []

    async def _fake_merge(t):
        merge_calls.append(t.identifier)
        orch.state.merged_pr_urls[t.id] = "sha-from-fallback"
        return True

    monkeypatch.setattr(orch, "_merge_pr", _fake_merge)

    await orch._poll_reviews()

    # Recursed into APPROVE path.
    assert ticket.status == TicketStatus.MERGED_GREEN
    assert merge_calls == [ticket.identifier]
    # Initial verdict recorded as COMMENTED_FALLBACK; recursion's APPROVE
    # overwrites it (last-writer-wins on state.review_verdicts).
    assert orch.state.review_verdicts[ticket.id] == "APPROVE"
    events = [e["kind"] for e in orch.state.events]
    assert "ticket_merged" in events


# ── Test 6: Silent review retries once, second silent → FAILED ──────────────


async def test_silent_review_retries_once(monkeypatch):
    mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=mesh)
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])
    # Completed review with no extractable verdict anywhere.
    orch._last_completed_by_id = {
        "review-1": {
            "id": "review-1",
            "status": "completed",
            "result": {"summary": "I had thoughts but did not commit to one"},
        },
    }

    async def _noop_linear(t, state_name):
        return None

    monkeypatch.setattr(orch, "_update_linear_state", _noop_linear)

    # Track _dispatch_review re-fires.
    redispatch_count = {"n": 0}
    original_dispatch = orch._dispatch_review

    async def _counting_dispatch(t):
        redispatch_count["n"] += 1
        # Simulate a new mesh task id so the next silent poll picks it up.
        t.review_task_id = f"review-{redispatch_count['n'] + 1}"
        orch.state.review_task_ids[t.id] = t.review_task_id

    monkeypatch.setattr(orch, "_dispatch_review", _counting_dispatch)

    # First poll: silent → retry fires, counter=1, status stays REVIEWING.
    await orch._poll_reviews()
    assert ticket.silent_review_retries == 1
    assert redispatch_count["n"] == 1
    assert ticket.status == TicketStatus.REVIEWING
    events1 = [e["kind"] for e in orch.state.events]
    assert "review_silent_retry" in events1

    # Stage a second silent completion using the NEW review task id.
    orch._last_completed_by_id = {
        ticket.review_task_id: {
            "id": ticket.review_task_id,
            "status": "completed",
            "result": {"summary": "still no verdict"},
        },
    }

    # Second poll: silent again → FAILED.
    await orch._poll_reviews()
    assert ticket.silent_review_retries == 2
    assert ticket.status == TicketStatus.FAILED
    events2 = [e["kind"] for e in orch.state.events]
    assert "review_silent_failed" in events2

    # sanity: reference the restored dispatcher so monkeypatch tidy-up is
    # deterministic even if the original ever gets accessed.
    assert callable(original_dispatch)


# ── Test 7: Merge idempotency on restart ───────────────────────────────────


async def test_merge_idempotency_on_restart(monkeypatch):
    orch = _mk_orchestrator()
    ticket = _mk_reviewing_ticket()
    _seed_graph(orch, [ticket])

    # Pretend the ticket was merged before the restart — sha is stashed
    # in state.merged_pr_urls but the status is still something we'd
    # nominally merge (simulating a daemon kill between GitHub PUT and
    # the MERGED_GREEN transition).
    orch.state.merged_pr_urls[ticket.id] = "pre-existing-sha"
    ticket.status = TicketStatus.MERGE_REQUESTED

    # Guard: github_merge_pr must NOT be invoked.
    merge_tool_calls: List[Dict[str, Any]] = []

    async def _sentinel_merge_tool(**kwargs):
        merge_tool_calls.append(kwargs)
        return {"ok": True, "sha": "new-sha-should-not-happen"}

    class _FakeSpec:
        handler = _sentinel_merge_tool

    import alfred_coo.tools as tools_mod

    monkeypatch.setitem(
        tools_mod.BUILTIN_TOOLS, "github_merge_pr", _FakeSpec
    )

    ok = await orch._merge_pr(ticket)

    assert ok is True
    # Double-merge guard short-circuited; no GitHub call.
    assert merge_tool_calls == []
    # SHA on state is untouched.
    assert orch.state.merged_pr_urls[ticket.id] == "pre-existing-sha"


# ── Test 8: State round-trip preserves review fields ────────────────────────


def test_state_roundtrip_preserves_review_fields():
    s = OrchestratorState(kickoff_task_id="k-rt")
    s.current_wave = 2
    s.review_task_ids = {
        "uA": "rev-111",
        "uB": "rev-222",
    }
    s.review_verdicts = {
        "uA": "APPROVE",
        "uB": "REQUEST_CHANGES",
    }
    s.merged_pr_urls = {
        "uA": "sha-aaa",
    }

    blob = s.to_json()
    # sanity — JSON is round-trippable.
    parsed = json.loads(blob)
    assert parsed["review_task_ids"] == {"uA": "rev-111", "uB": "rev-222"}
    assert parsed["merged_pr_urls"] == {"uA": "sha-aaa"}

    restored = OrchestratorState.from_json(blob)
    assert restored.review_task_ids == s.review_task_ids
    assert restored.review_verdicts == s.review_verdicts
    assert restored.merged_pr_urls == s.merged_pr_urls
    # Forward-compat sanity: an old blob without any of the new keys
    # still loads cleanly with empty dicts.
    legacy_blob = json.dumps(
        {
            "kickoff_task_id": "k-legacy",
            "current_wave": 0,
            "ticket_status": {},
            "cumulative_spend_usd": 0.0,
        }
    )
    legacy = OrchestratorState.from_json(legacy_blob)
    assert legacy.review_task_ids == {}
    assert legacy.review_verdicts == {}
    assert legacy.merged_pr_urls == {}


# ── Bonus: _extract_verdict unit coverage (keeps the helper honest) ─────────


def test_extract_verdict_priority_order():
    # tool_calls wins over summary when both disagree.
    r = {
        "tool_calls": [
            {"name": "pr_review", "result": {"state": "REQUEST_CHANGES"}}
        ],
        "summary": "APPROVE",
    }
    assert (
        AutonomousBuildOrchestrator._extract_verdict(r) == "REQUEST_CHANGES"
    )
    # Falls back to summary when tool_calls has no pr_review entry.
    r2 = {
        "tool_calls": [{"name": "propose_pr", "result": {}}],
        "summary": "APPROVE it all looks good",
    }
    assert AutonomousBuildOrchestrator._extract_verdict(r2) == "APPROVE"
    # follow_up_tasks as last resort.
    r3 = {"follow_up_tasks": ["please REQUEST_CHANGES on the auth path"]}
    assert (
        AutonomousBuildOrchestrator._extract_verdict(r3) == "REQUEST_CHANGES"
    )
    # Silent.
    assert AutonomousBuildOrchestrator._extract_verdict({}) is None
    assert (
        AutonomousBuildOrchestrator._extract_verdict(
            {"summary": "I am mostly fine with this"}
        )
        is None
    )


def test_parse_fallback_verdict_finds_intended_event():
    rec = {
        "result": {
            "tool_calls": [
                {
                    "name": "pr_review",
                    "result": {
                        "state": "COMMENTED_FALLBACK",
                        "intended_event": "APPROVE",
                        "fallback_reason": "self-authored",
                    },
                }
            ],
        },
    }
    assert (
        AutonomousBuildOrchestrator._parse_fallback_verdict(rec) == "APPROVE"
    )
    # No intended_event → None.
    rec2 = {
        "result": {
            "tool_calls": [
                {
                    "name": "pr_review",
                    "result": {"state": "COMMENTED_FALLBACK"},
                }
            ]
        }
    }
    assert (
        AutonomousBuildOrchestrator._parse_fallback_verdict(rec2) is None
    )


# Note: pyproject.toml sets `asyncio_mode = "auto"` so async test functions
# are picked up automatically without a per-test `@pytest.mark.asyncio`.
