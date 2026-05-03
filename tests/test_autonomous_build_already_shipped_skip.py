"""SAL-4148 substrate doctor signal: orchestrator already-shipped-skip
dispatch predicate (companion to SAL-4115).

Acceptance criteria (verbatim from SAL-4148):

1. New function ``_ticket_has_main_commit(ticket, repo)`` exists in
   ``src/alfred_coo/autonomous_build/orchestrator.py`` with docstring and
   type hints.
2. ``_dispatch_wave()`` calls ``_ticket_has_main_commit()`` for each
   ticket; if True, ticket is skipped with structured log
   ``already-shipped-skip ticket=<key> repo=<r> commit=<sha> action=skip``.
3. Skip is gated by env var ``ALREADY_SHIPPED_SKIP_ENABLED`` (default
   ``true``); set ``false`` to disable predicate without code change.
4. Tests:
   * Unit: mocked git log, assert skip when commit found.
   * Unit: mocked git log, assert dispatch when no commit.
   * Unit: env var false → predicate inert.
   * Integration: drives ``_dispatch_wave(0)`` with stubbed orchestrator
     state; asserts structured log emission.
   * Mutation test: remove the predicate call; assert the integration
     test fails. (Performed manually during development; documented in
     the PR body, not committed as a separate test.)

The dispatch-site logic this test pins lives in
``AutonomousBuildOrchestrator._dispatch_wave``: the SAL-4148 patched
branch sits between the merged-PR-skip (Gap 4 / SAL-3037) and the
open-PR-awaiting-review skip (SAL-3038 / SAL-4115). The predicate
short-circuits to ``MERGED_GREEN`` + Linear "Done" so the ticket is
treated as already-shipped and dispatch stops.

Behavioural APE/V (per ``feedback_apev_must_be_behavioral``): the
integration test does NOT just assert a structured-log substring is
present; it also asserts that ``_dispatch_child`` is NOT called for the
shipped ticket, that the state event is recorded, and that the ticket's
``MERGED_GREEN`` status is set. Together these cover the actual
behaviour the operator cares about: zero builders fired for already-
shipped work.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred_coo.autonomous_build import orchestrator as orch_mod
from alfred_coo.autonomous_build.graph import (
    Ticket,
    TicketGraph,
    TicketStatus,
)
from alfred_coo.autonomous_build.orchestrator import (
    ALREADY_SHIPPED_GIT_LOG_LIMIT,
    AutonomousBuildOrchestrator,
    _resolve_already_shipped_repo_root,
    _resolve_already_shipped_skip_enabled,
    _ticket_has_main_commit,
)
from alfred_coo.autonomous_build.state import OrchestratorState


# ── Fakes (pattern from test_existing_pr_skip_sal4115.py) ────────────────


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
        "id": "kick-sal4148",
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


def _mk_completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["git", "log"], returncode=returncode, stdout=stdout, stderr="",
    )


# ── Acceptance #3 — env-var resolver ──────────────────────────────────────


def test_resolve_already_shipped_skip_enabled_default_true(monkeypatch):
    """Default behaviour is ENABLED — operator must opt-out explicitly."""
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    assert _resolve_already_shipped_skip_enabled() is True


@pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "on"])
def test_resolve_already_shipped_skip_enabled_truthy(monkeypatch, val):
    monkeypatch.setenv("ALREADY_SHIPPED_SKIP_ENABLED", val)
    assert _resolve_already_shipped_skip_enabled() is True


@pytest.mark.parametrize("val", ["false", "False", "FALSE", "0", "no", "off", "anything-else"])
def test_resolve_already_shipped_skip_enabled_falsy(monkeypatch, val):
    """Operator opt-out: anything that is NOT a canonical truthy value
    disables the predicate. Lets us tighten or relax during incident
    remediation without a redeploy.
    """
    monkeypatch.setenv("ALREADY_SHIPPED_SKIP_ENABLED", val)
    assert _resolve_already_shipped_skip_enabled() is False


def test_resolve_already_shipped_repo_root_default(monkeypatch):
    monkeypatch.delenv("ALREADY_SHIPPED_REPO_ROOT", raising=False)
    assert _resolve_already_shipped_repo_root() == "/opt/alfred-coo/repos"


def test_resolve_already_shipped_repo_root_override(monkeypatch):
    monkeypatch.setenv("ALREADY_SHIPPED_REPO_ROOT", "/custom/path")
    assert _resolve_already_shipped_repo_root() == "/custom/path"


# ── Unit: predicate behaviour with mocked subprocess ──────────────────────


def test_ticket_has_main_commit_returns_sha_when_log_populated(tmp_path, monkeypatch):
    """Acceptance #1 (skip when commit found): given a stdout containing
    a SHA + subject line, the predicate returns the SHA so the dispatch
    site can log + skip.
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()
    captured: dict[str, Any] = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _mk_completed("abc1234 fix(SAL-4148): land predicate\n")

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=fake_runner,
    )
    assert sha == "abc1234"
    # Behavioural assertion — the predicate must invoke the canonical
    # git-log incantation against the repo's local clone, NOT a GitHub
    # round-trip. This pins SAL-4148's design contract: filesystem-first.
    assert captured["cmd"][0] == "git"
    assert captured["cmd"][1] == "log"
    assert captured["cmd"][2] == "main"
    assert "--grep" in captured["cmd"]
    assert "SAL-4148" in captured["cmd"]
    assert "--oneline" in captured["cmd"]
    assert str(ALREADY_SHIPPED_GIT_LOG_LIMIT) in captured["cmd"]
    assert captured["kwargs"]["cwd"] == str(repo_dir)


def test_ticket_has_main_commit_returns_none_when_log_empty(tmp_path, monkeypatch):
    """Acceptance: dispatch when no commit. Empty stdout → ``None`` →
    dispatch loop falls through to ``_dispatch_child``.
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()

    def fake_runner(cmd, **kwargs):
        return _mk_completed("")

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=fake_runner,
    )
    assert sha is None


def test_ticket_has_main_commit_env_disabled_short_circuits(tmp_path, monkeypatch):
    """Acceptance #3: env var ``false`` makes the predicate inert. The
    runner MUST NOT be invoked — pinning that the env-gate is checked
    before the subprocess round-trip (cheap, no side-effects). This
    matters for the operator-override case where the predicate is
    over-firing and we want to disable WITHOUT a code deploy.
    """
    monkeypatch.setenv("ALREADY_SHIPPED_SKIP_ENABLED", "false")
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()

    def explosive_runner(cmd, **kwargs):
        raise AssertionError(
            "runner must NOT be invoked when ALREADY_SHIPPED_SKIP_ENABLED=false"
        )

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=explosive_runner,
    )
    assert sha is None


def test_ticket_has_main_commit_repo_missing_returns_none(tmp_path, monkeypatch):
    """If the repo is not checked out locally, the predicate fails-OPEN
    to dispatch. The orchestrator's ``_verify_wave_hints`` /
    ``REPO_MISSING`` path handles the genuinely-missing case via a
    grounding-gap Linear issue; SAL-4148 stays a strict additional skip
    layer rather than duplicating that escalation.
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)

    def explosive_runner(cmd, **kwargs):
        raise AssertionError("runner must not run when repo dir is missing")

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/never-existed",
        repo_root=str(tmp_path), runner=explosive_runner,
    )
    assert sha is None


def test_ticket_has_main_commit_subprocess_error_fails_open(tmp_path, monkeypatch):
    """Transient git failure must NOT cause an over-skip — fail-OPEN to
    dispatch. ``OSError`` / ``SubprocessError`` paths return ``None`` so
    the dispatch loop proceeds rather than getting wedged on a busted
    git binary.
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()

    def boom_runner(cmd, **kwargs):
        raise OSError("git binary corrupt")

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=boom_runner,
    )
    assert sha is None


def test_ticket_has_main_commit_nonzero_returncode_returns_none(tmp_path, monkeypatch):
    """``git log`` non-zero exit (e.g. ``main`` ref absent in this clone)
    → returns ``None``. Fail-OPEN behaviour mirrors the OSError path.
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()

    def fake_runner(cmd, **kwargs):
        return _mk_completed("abc1234 something\n", returncode=128)

    sha = _ticket_has_main_commit(
        "SAL-4148", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=fake_runner,
    )
    assert sha is None


def test_ticket_has_main_commit_empty_inputs_short_circuit(tmp_path, monkeypatch):
    """Empty ``ticket_ident`` or ``owner_repo`` → ``None`` without
    invoking the runner. Defensive against unhydrated tickets."""
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)

    def explosive_runner(cmd, **kwargs):
        raise AssertionError("runner must not run when inputs are empty")

    assert _ticket_has_main_commit(
        "", "salucallc/alfred-coo-svc",
        repo_root=str(tmp_path), runner=explosive_runner,
    ) is None
    assert _ticket_has_main_commit(
        "SAL-4148", "",
        repo_root=str(tmp_path), runner=explosive_runner,
    ) is None


# ── Integration: dispatch-loop end-to-end ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_wave_skips_already_shipped_ticket(
    monkeypatch, caplog, tmp_path,
):
    """Behavioural APE/V (acceptance #2 + #4 — drives ``_dispatch_wave(0)``):

    * Stub orchestrator state with ONE ticket whose key has a main commit.
    * Patch the predicate's runner to return the SHA.
    * Patch ``_dispatch_child`` to a mock so we can assert it was NEVER called.
    * Drive ``_dispatch_wave(0)`` for one tick.
    * Assert:
      - ``_dispatch_child`` was NOT called for the skipped ticket (#4 — only
        zero dispatches, since the ticket is already-shipped).
      - The structured log line is emitted EXACTLY ONCE per ticket.
      - The ``already_shipped_skip`` event is recorded on
        ``OrchestratorState``.
      - The ticket flips to ``MERGED_GREEN`` (per the dispatch-site
        terminal: already-shipped == done, NOT awaiting review).
    """
    monkeypatch.delenv("ALREADY_SHIPPED_SKIP_ENABLED", raising=False)
    monkeypatch.setenv("ALREADY_SHIPPED_REPO_ROOT", str(tmp_path))
    repo_dir = tmp_path / "alfred-coo-svc"
    repo_dir.mkdir()

    caplog.set_level(logging.INFO, logger="alfred_coo.autonomous_build.orchestrator")

    orch = _mk_orch()
    orch.linear_project_id = "11111111-1111-1111-1111-111111111111"

    t = _t("u-4148", "SAL-4148-FIXTURE", code="OPS-01")
    t.status = TicketStatus.PENDING
    _seed_graph(orch, [t])
    orch.state = OrchestratorState(kickoff_task_id="kick-sal4148")

    # Patch the predicate's subprocess.run override to return a hit.
    def fake_runner(cmd, **kwargs):
        return _mk_completed("deadbeef fix(SAL-4148-FIXTURE): land work\n")

    monkeypatch.setattr(
        orch_mod.subprocess, "run", fake_runner,
    )

    # Stub the target-hint resolver so the predicate sees a concrete repo.
    fake_hint = MagicMock()
    fake_hint.owner = "salucallc"
    fake_hint.repo = "alfred-coo-svc"
    monkeypatch.setattr(
        orch_mod, "_resolve_target_hint",
        lambda ticket: (fake_hint, "body"),
    )

    # Defang side-effect helpers so the dispatch tick doesn't try to hit
    # GitHub / Linear / mesh. Each is replaced with a no-op stub so the
    # only behaviour exercised is the SAL-4148 skip branch.
    orch._ticket_has_merged_pr = AsyncMock(return_value=None)
    orch._ticket_has_open_pr_awaiting_review = AsyncMock(return_value=None)
    orch._update_linear_state = AsyncMock(return_value=None)
    orch._reconcile_orphan_active = AsyncMock(return_value=None)
    orch._verify_wave_hints = AsyncMock(return_value=None)
    orch._mark_repo_missing_tickets = AsyncMock(return_value=None)
    orch._maybe_post_target_repo_drift_alert = AsyncMock(return_value=None)
    orch._poll_children = AsyncMock(return_value=None)
    orch._poll_reviews = AsyncMock(return_value=None)
    orch._check_budget = AsyncMock(return_value=None)
    orch._status_tick = AsyncMock(return_value=None)
    orch._stall_watcher = AsyncMock(return_value=None)
    orch._check_cancel_signal = AsyncMock(return_value=None)
    orch._wait_for_wave_gate = AsyncMock(return_value=None)
    orch._maybe_ss08_gate = AsyncMock(return_value=True)
    orch._dispatch_child = AsyncMock(return_value=None)
    orch._select_ready = lambda wave_tickets, in_flight: list(wave_tickets)
    orch._in_flight_for_wave = lambda wave_n: []
    orch._epic_in_flight = lambda epic, in_flight: 0
    orch._file_collision_for = lambda ticket, in_flight: None
    orch.disable_file_collision_check = True

    # Force the dispatch-wave loop to exit after exactly one tick by
    # flipping the ticket terminal at the end of the tick.
    original_poll = orch._poll_children

    async def one_tick_then_exit(*args, **kwargs):
        # After the dispatch fires (or skips), mark wave terminal so the
        # loop's terminal-check breaks.
        for tk in orch.graph.nodes.values():
            if tk.status not in (
                TicketStatus.MERGED_GREEN, TicketStatus.FAILED,
                TicketStatus.ESCALATED,
            ):
                # Already MERGED_GREEN from the skip path; nothing else
                # to do.
                pass
        return await original_poll(*args, **kwargs)

    orch._poll_children = one_tick_then_exit  # type: ignore[assignment]

    # Drive the dispatch loop. The wave loop's terminal predicate exits
    # when every ticket is in a terminal state — MERGED_GREEN counts.
    # We bound the test by patching the inner sleep to a no-op so the
    # loop spins fast.
    import asyncio as _asyncio

    # Bound the wave loop: yield control to wait_for via a real (zero-
    # length) sleep so the timeout below can fire if the dispatch loop
    # gets stuck (e.g. mutation test scenario where the predicate is
    # commented out, the ticket never reaches MERGED_GREEN, and the
    # loop would otherwise spin forever).
    real_sleep = _asyncio.sleep

    async def yield_sleep(_):
        # Always yield via the real event loop so wait_for's deadline
        # is enforceable. 0-second sleep keeps the test fast in the
        # green case (predicate fires + ticket terminates immediately).
        await real_sleep(0)

    monkeypatch.setattr(_asyncio, "sleep", yield_sleep)

    # Skip the wave-gate filter logic since `_filter_out_of_scope_tickets`
    # depends on `ORCHESTRATOR_REPO_SCOPE` registration. Replace with a
    # passthrough.
    orch._filter_out_of_scope_tickets = lambda tickets, wave_n: (list(tickets), [])

    # 5s timeout is generous: the green-path dispatch tick completes in
    # ~5ms; if we cross 5s we are stuck (mutation-test signal).
    await _asyncio.wait_for(orch._dispatch_wave(0), timeout=5.0)

    # ── Behavioural APE/V assertions ─────────────────────────────────────
    # 1. _dispatch_child was NEVER called for the shipped ticket.
    assert orch._dispatch_child.await_count == 0, (
        "BEHAVIOURAL FAIL: orchestrator dispatched a builder for an "
        "already-shipped ticket. SAL-4148 predicate should have skipped it."
    )

    # 2. Structured log line emitted (acceptance #2 verbatim format).
    log_lines = [r.getMessage() for r in caplog.records]
    matching = [
        ln for ln in log_lines
        if ln.startswith("already-shipped-skip ticket=SAL-4148-FIXTURE")
        and "repo=salucallc/alfred-coo-svc" in ln
        and "commit=deadbeef" in ln
        and "action=skip" in ln
    ]
    assert len(matching) == 1, (
        f"expected exactly one already-shipped-skip log line, got "
        f"{len(matching)}; all lines: {log_lines}"
    )

    # 3. State event recorded.
    matching_events = [
        e for e in orch.state.events
        if e.get("kind") == "already_shipped_skip"
    ]
    assert len(matching_events) == 1, (
        f"expected exactly one already_shipped_skip state event, got "
        f"{len(matching_events)}; all events: {orch.state.events}"
    )
    payload = matching_events[0]
    assert payload.get("identifier") == "SAL-4148-FIXTURE"
    assert payload.get("repo") == "salucallc/alfred-coo-svc"
    assert payload.get("commit") == "deadbeef"
    assert payload.get("action") == "skip"

    # 4. Ticket flipped MERGED_GREEN — dispatch terminal, no future
    #    dispatch wave will re-pick this up.
    assert t.status == TicketStatus.MERGED_GREEN

    # 5. Linear sync called with "Done".
    orch._update_linear_state.assert_awaited_once_with(t, "Done")
