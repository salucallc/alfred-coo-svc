"""Tests for the restart_stalled_chains playbook.

Covers:
* History persistence: load tolerates missing/corrupt files; save prunes
  outside the window.
* `_recent_attempts` filters by sliding window.
* `_chain_alive_for_project` recognises pending tasks AND fresh-claimed
  tasks; ignores stale heartbeats; ignores tasks for other projects.
* `_count_backlog_tickets` counts only Backlog state.
* End-to-end execute(): no backlog → skip; chain alive → skip; chain
  stalled + budget available → queue kickoff + record attempt; budget
  exhausted → escalate (error in result, no kickoff queued); dry-run
  emits notable but no kickoff; missing kwargs → result.errors,
  no raise.
* Default registry includes the new playbook.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from alfred_coo.autonomous_build.playbooks import (
    DEFAULT_PLAYBOOKS,
    PlaybookResult,
    RestartStalledChainsPlaybook,
)
from alfred_coo.autonomous_build.playbooks.restart_stalled_chains import (
    DEFAULT_ATTEMPT_BUDGET,
    DEFAULT_ATTEMPT_COOLDOWN_SEC,
    DEFAULT_ATTEMPT_WINDOW_SEC,
    DEFAULT_HEARTBEAT_FRESHNESS_SEC,
    _chain_alive_for_project,
    _count_backlog_tickets,
    _load_history,
    _recent_attempts,
    _save_history,
)


# ── Pure helpers: history persistence ──────────────────────────────────────


def test_load_history_returns_empty_on_missing_file(tmp_path):
    h = _load_history(str(tmp_path / "no-such-file.json"))
    assert h == {}


def test_load_history_tolerates_corrupt_json(tmp_path):
    p = tmp_path / "history.json"
    p.write_text("this is not json {", encoding="utf-8")
    h = _load_history(str(p))
    assert h == {}


def test_save_then_load_round_trip(tmp_path):
    p = str(tmp_path / "history.json")
    _save_history(p, {"proj-1": [100.0, 200.0]}, now=300.0)
    loaded = _load_history(p)
    # Both within window (default 3600s).
    assert loaded == {"proj-1": [100.0, 200.0]}


def test_save_history_prunes_entries_outside_window(tmp_path):
    """Entries older than ``window_sec`` are dropped on save so the
    history file doesn't grow unboundedly across daemon lifetimes."""
    p = str(tmp_path / "history.json")
    now = 10_000.0
    _save_history(
        p,
        {"proj-1": [100.0, 8000.0, 9500.0]},  # 100 is outside 1h window
        now=now,
        window_sec=3600,
    )
    loaded = _load_history(p)
    assert loaded == {"proj-1": [8000.0, 9500.0]}


def test_save_history_drops_empty_projects_after_prune(tmp_path):
    p = str(tmp_path / "history.json")
    _save_history(
        p,
        {"proj-old": [100.0], "proj-fresh": [9500.0]},
        now=10_000.0,
        window_sec=3600,
    )
    loaded = _load_history(p)
    assert loaded == {"proj-fresh": [9500.0]}


# ── _recent_attempts ──────────────────────────────────────────────────────


def test_recent_attempts_filters_by_window():
    history = {"p": [100.0, 500.0, 950.0, 990.0]}
    out = _recent_attempts(history, "p", now=1000.0, window_sec=100)
    assert out == [950.0, 990.0]


def test_recent_attempts_returns_empty_for_unknown_project():
    assert _recent_attempts({}, "no-such-project", now=1.0, window_sec=10) == []


# ── _chain_alive_for_project ─────────────────────────────────────────────


def _now_iso(offset_sec: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_sec)).isoformat()


class _FakeMesh:
    def __init__(self, pending=None, claimed=None):
        self._pending = pending or []
        self._claimed = claimed or []

    async def list_tasks(self, *, status=None, limit=200):
        if status == "pending":
            return list(self._pending)
        if status == "claimed":
            return list(self._claimed)
        return []


def _ab_task(project_id: str, *, heartbeat_offset: float = 0.0) -> dict:
    """Build a fake autonomous-build-a mesh task for the given project."""
    return {
        "title": "[persona:autonomous-build-a] some title (auto-restart)",
        "description": json.dumps({"linear_project_id": project_id}),
        "heartbeat_at": _now_iso(heartbeat_offset),
        "claimed_at": _now_iso(heartbeat_offset),
    }


@pytest.mark.asyncio
async def test_chain_alive_when_pending_kickoff_queued():
    """A pending autonomous-build-a kickoff for the project means the
    daemon will pick it up — alive."""
    pid = "proj-1"
    mesh = _FakeMesh(pending=[_ab_task(pid)])
    assert await _chain_alive_for_project(mesh, project_id=pid, now=time.time()) is True


@pytest.mark.asyncio
async def test_chain_alive_when_recently_heartbeat_claimed():
    pid = "proj-1"
    mesh = _FakeMesh(claimed=[_ab_task(pid, heartbeat_offset=-30)])  # 30s ago
    assert await _chain_alive_for_project(mesh, project_id=pid, now=time.time()) is True


@pytest.mark.asyncio
async def test_chain_dead_when_heartbeat_too_stale():
    """Heartbeat older than ``freshness_sec`` → chain considered dead."""
    pid = "proj-1"
    # 2h ago; default freshness 30 min.
    mesh = _FakeMesh(claimed=[_ab_task(pid, heartbeat_offset=-7200)])
    assert await _chain_alive_for_project(mesh, project_id=pid, now=time.time()) is False


@pytest.mark.asyncio
async def test_chain_dead_when_no_matching_project():
    """Tasks for OTHER projects don't count."""
    mesh = _FakeMesh(claimed=[_ab_task("proj-other")])
    assert await _chain_alive_for_project(
        mesh, project_id="proj-1", now=time.time(),
    ) is False


@pytest.mark.asyncio
async def test_chain_dead_ignores_non_autonomous_build_tasks():
    """Doctor tasks etc. don't count as the autonomous-build chain."""
    mesh = _FakeMesh(claimed=[
        {
            "title": "[persona:alfred-doctor] surveillance tick",
            "description": json.dumps({"linear_project_id": "proj-1"}),
            "heartbeat_at": _now_iso(0),
        },
    ])
    assert await _chain_alive_for_project(
        mesh, project_id="proj-1", now=time.time(),
    ) is False


@pytest.mark.asyncio
async def test_chain_dead_when_task_description_not_json():
    """Malformed description shouldn't crash the check; just skip the
    task and continue."""
    mesh = _FakeMesh(claimed=[
        {
            "title": "[persona:autonomous-build-a] x",
            "description": "not-valid-json",
            "heartbeat_at": _now_iso(0),
        },
    ])
    assert await _chain_alive_for_project(
        mesh, project_id="proj-1", now=time.time(),
    ) is False


# ── _count_backlog_tickets ───────────────────────────────────────────────


class _FakePostClient:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers, content):
        self.calls.append({"url": url, "vars": json.loads(content.decode()).get("variables")})

        class _Resp:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        return _Resp(self.response)


@pytest.mark.asyncio
async def test_count_backlog_only_counts_backlog_state():
    payload = {
        "data": {
            "project": {
                "issues": {
                    "nodes": [
                        {"state": {"name": "Backlog"}},
                        {"state": {"name": "Backlog"}},
                        {"state": {"name": "Done"}},
                        {"state": {"name": "Cancelled"}},
                        {"state": {"name": "In Progress"}},
                        {"state": {"name": "Backlog"}},
                    ],
                }
            }
        }
    }
    n = await _count_backlog_tickets(
        _FakePostClient(payload),
        linear_api_key="k",
        project_id="proj-1",
    )
    assert n == 3


# ── End-to-end execute() ─────────────────────────────────────────────────


class _MeshWithCreate(_FakeMesh):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created: list[dict] = []

    async def create_task(self, *, title, description, from_session_id):
        rec = {"title": title, "description": description, "from_session_id": from_session_id}
        self.created.append(rec)
        return {"id": f"new-task-{len(self.created):02d}"}


def _patch_httpx(monkeypatch, payload: dict) -> _FakePostClient:
    fake = _FakePostClient(payload)

    def factory(*args, **kwargs):
        return fake

    monkeypatch.setattr(
        "alfred_coo.autonomous_build.playbooks.restart_stalled_chains.httpx.AsyncClient",
        factory,
    )
    return fake


def _payload_with_backlog(n_backlog: int) -> dict:
    nodes = [{"state": {"name": "Backlog"}} for _ in range(n_backlog)]
    nodes.extend([{"state": {"name": "Done"}} for _ in range(3)])
    return {"data": {"project": {"issues": {"nodes": nodes}}}}


@pytest.mark.asyncio
async def test_execute_skips_projects_without_backlog(monkeypatch, tmp_path):
    """Project with 0 Backlog → no action, not counted as candidate."""
    pb = RestartStalledChainsPlaybook(
        projects={"only": "proj-1"},
        history_path=str(tmp_path / "history.json"),
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(0))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)
    assert res.candidates_found == 0
    assert res.actions_taken == 0
    assert mesh.created == []


@pytest.mark.asyncio
async def test_execute_skips_when_chain_alive(monkeypatch, tmp_path):
    """Backlog non-empty BUT chain already alive → skipped (no double-fire)."""
    pid = "proj-1"
    pb = RestartStalledChainsPlaybook(
        projects={"only": pid},
        history_path=str(tmp_path / "history.json"),
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(5))
    mesh = _MeshWithCreate(claimed=[_ab_task(pid, heartbeat_offset=-30)])
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)
    assert res.candidates_found == 0
    assert res.actions_skipped == 1
    assert mesh.created == []


@pytest.mark.asyncio
async def test_execute_restarts_stalled_chain(monkeypatch, tmp_path):
    """Backlog + chain dead + budget available → kickoff queued."""
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    pb = RestartStalledChainsPlaybook(
        projects={"Cockpit-UX": pid},
        history_path=history_path,
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)
    assert res.candidates_found == 1
    assert res.actions_taken == 1
    assert len(mesh.created) == 1
    new_task = mesh.created[0]
    payload = json.loads(new_task["description"])
    assert payload["linear_project_id"] == pid
    assert "auto_restarted_by" in payload
    assert "Cockpit-UX" in new_task["title"]
    # History must record this attempt for next-tick budgeting.
    history = _load_history(history_path)
    assert pid in history
    assert len(history[pid]) == 1


@pytest.mark.asyncio
async def test_execute_dry_run_does_not_mutate(monkeypatch, tmp_path):
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    pb = RestartStalledChainsPlaybook(
        projects={"Test-Proj": pid},
        history_path=history_path,
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=True, mesh=mesh)
    assert res.candidates_found == 1
    assert res.actions_taken == 0
    assert any("would restart Test-Proj" in n for n in res.notable)
    assert mesh.created == []
    # Dry-run also doesn't record an attempt — budget is unaffected.
    history = _load_history(history_path)
    assert pid not in history


@pytest.mark.asyncio
async def test_execute_escalates_when_budget_exhausted(monkeypatch, tmp_path):
    """After ``DEFAULT_ATTEMPT_BUDGET`` restarts in the window, the
    playbook STOPS auto-restarting and surfaces the project to the
    operator. The signal lands in ``result.escalations`` (designed
    human-needed signal), NOT ``result.errors`` (real failures), so
    Phase 3b deviation detection won't misclassify a healthy
    escalation as a playbook regression."""
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    now = time.time()
    # Pre-populate history with budget-many recent attempts.
    seed = {pid: [now - i for i in range(DEFAULT_ATTEMPT_BUDGET)]}
    import json as _j
    from pathlib import Path as _P
    _P(history_path).write_text(_j.dumps(seed), encoding="utf-8")

    pb = RestartStalledChainsPlaybook(
        projects={"MSSP-Fed": pid},
        history_path=history_path,
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)

    # No restart attempt this tick.
    assert res.actions_taken == 0
    assert mesh.created == []
    # Escalation surfaces the project + backlog count + budget exhaustion.
    assert any(
        "MSSP-Fed" in e and "budget exhausted" in e
        for e in res.escalations
    ), res.escalations
    # Errors stays empty — this is a designed signal, not a real failure.
    assert res.errors == []


@pytest.mark.asyncio
async def test_execute_returns_error_when_mesh_missing(tmp_path):
    """A misconfigured doctor that doesn't pass mesh kwarg shouldn't
    crash — the playbook reports the missing kwarg and returns."""
    pb = RestartStalledChainsPlaybook(
        projects={"x": "p"},
        history_path=str(tmp_path / "h.json"),
    )
    res = await pb.execute(linear_api_key="k", dry_run=False)
    assert res.actions_taken == 0
    assert any("mesh kwarg missing" in e for e in res.errors)


@pytest.mark.asyncio
async def test_execute_returns_error_when_linear_key_missing(tmp_path):
    pb = RestartStalledChainsPlaybook(
        projects={"x": "p"},
        history_path=str(tmp_path / "h.json"),
    )
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="", dry_run=False, mesh=mesh)
    assert res.actions_taken == 0
    assert any("linear_api_key missing" in e for e in res.errors)


@pytest.mark.asyncio
async def test_execute_handles_per_project_failures_independently(
    monkeypatch, tmp_path,
):
    """Linear backlog query failure for one project shouldn't stop the
    loop — other projects still get their chance."""
    pid_ok = "proj-ok"
    pid_bad = "proj-bad"
    history_path = str(tmp_path / "h.json")
    pb = RestartStalledChainsPlaybook(
        projects={"good": pid_ok, "bad": pid_bad},
        history_path=history_path,
    )

    class _PartialFailureClient:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers, content):
            body = json.loads(content.decode())
            pid = (body.get("variables") or {}).get("pid")
            self.calls.append(pid)
            if pid == pid_bad:
                raise httpx.ConnectError("simulated network glitch")

            class _Resp:
                def json(self):
                    return _payload_with_backlog(5)

            return _Resp()

    fake = _PartialFailureClient()
    monkeypatch.setattr(
        "alfred_coo.autonomous_build.playbooks.restart_stalled_chains.httpx.AsyncClient",
        lambda *a, **kw: fake,
    )
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)

    # The good project ran end-to-end + queued a kickoff; the bad
    # project's failure is recorded.
    assert res.actions_taken == 1
    assert any("bad" in e and "backlog_query_failed" in e for e in res.errors)


# ── PlaybookResult escalations rendering ──────────────────────────────────


def test_playbook_result_is_silent_treats_escalations_as_signal():
    """A budget-exhaustion escalation must NOT collapse to ``Substrate
    quiet`` in the digest. Without this the operator would never see
    the very signal the escalation was designed to surface."""
    pr = PlaybookResult(kind="restart_stalled_chains", dry_run=False)
    assert pr.is_silent() is True
    pr.escalations.append("MSSP-Fed: budget exhausted")
    assert pr.is_silent() is False


def test_playbook_result_render_digest_separates_escalations_from_errors():
    """Digest line: head shows ``needs_human=N`` distinctly from
    ``errors=N`` so the operator (and Phase 3b deviation detection)
    can tell a designed escalation from a real playbook regression."""
    pr = PlaybookResult(
        kind="restart_stalled_chains",
        candidates_found=0,
        actions_taken=0,
        dry_run=False,
        escalations=["MSSP-Fed: stalled with 7 backlog tickets; budget exhausted"],
        errors=["mesh_check_failed: ConnectError"],
    )
    lines = pr.render_digest_lines()
    head = lines[0]
    assert "needs_human=1" in head
    assert "errors=1" in head
    # Escalation rendered as a "needs human" line, distinct from the
    # error tail.
    body = "\n".join(lines[1:])
    assert "needs human:" in body
    assert "MSSP-Fed" in body


# ── Substrate task #87 (2026-05-02): per-project cooldown ─────────────────


@pytest.mark.asyncio
async def test_execute_cooldown_active_skips_when_recent_attempt(
    monkeypatch, tmp_path
):
    """A recent attempt within ``attempt_cooldown_sec`` (default 600s)
    blocks a fresh restart even when budget is available. Substrate task
    #87: doctor's 5-minute surveillance tick must NOT burn the full
    budget in 15 minutes when a chain is genuinely dead — leaves
    operator no time to intervene before escalation."""
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    now = time.time()
    # One attempt 3 minutes ago — well inside the 10-minute cooldown.
    seed = {pid: [now - 180]}
    import json as _j
    from pathlib import Path as _P
    _P(history_path).write_text(_j.dumps(seed), encoding="utf-8")

    pb = RestartStalledChainsPlaybook(
        projects={"Cockpit-UX": pid},
        history_path=history_path,
        # default attempt_cooldown_sec=600
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)

    # Cooldown should suppress the restart.
    assert res.actions_taken == 0, "cooldown must block fresh kickoff"
    assert mesh.created == [], "no kickoff queued during cooldown"
    assert res.actions_skipped == 1
    # Notable should explain the cooldown so operator knows why no restart.
    assert any("cooldown active" in n for n in res.notable), (
        f"expected cooldown notable, got {res.notable!r}"
    )


@pytest.mark.asyncio
async def test_execute_cooldown_expired_allows_restart(
    monkeypatch, tmp_path
):
    """Once cooldown expires (last attempt > attempt_cooldown_sec ago),
    a fresh restart proceeds normally. Verifies cooldown is a sliding
    window, not a permanent block."""
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    now = time.time()
    # One attempt 15 minutes ago — past the 10-minute cooldown.
    seed = {pid: [now - 900]}
    import json as _j
    from pathlib import Path as _P
    _P(history_path).write_text(_j.dumps(seed), encoding="utf-8")

    pb = RestartStalledChainsPlaybook(
        projects={"Cockpit-UX": pid},
        history_path=history_path,
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)

    assert res.actions_taken == 1, "cooldown expired → restart should fire"
    assert len(mesh.created) == 1
    # History must record TWO attempts now: the seeded one + the new one.
    history = _load_history(history_path)
    assert len(history[pid]) == 2


@pytest.mark.asyncio
async def test_execute_cooldown_zero_disables_check(monkeypatch, tmp_path):
    """``attempt_cooldown_sec=0`` disables the cooldown — a freshly-
    attempted project can be restarted immediately if budget allows.
    Used in tests + emergency operator overrides."""
    pid = "proj-1"
    history_path = str(tmp_path / "history.json")
    now = time.time()
    seed = {pid: [now - 30]}  # 30s ago — would normally block
    import json as _j
    from pathlib import Path as _P
    _P(history_path).write_text(_j.dumps(seed), encoding="utf-8")

    pb = RestartStalledChainsPlaybook(
        projects={"Cockpit-UX": pid},
        history_path=history_path,
        attempt_cooldown_sec=0,
    )
    _patch_httpx(monkeypatch, _payload_with_backlog(7))
    mesh = _MeshWithCreate()
    res = await pb.execute(linear_api_key="k", dry_run=False, mesh=mesh)

    assert res.actions_taken == 1, "cooldown=0 should not block restart"


def test_default_attempt_cooldown_is_ten_minutes():
    """Cooldown default lands at 10 minutes (600s). Doctor surveillance
    tick is 5 minutes; cooldown ≥2× tick interval ensures budget can't
    drain in fewer than 30 minutes when a chain is genuinely dead."""
    assert DEFAULT_ATTEMPT_COOLDOWN_SEC == 600


# ── Default registry ──────────────────────────────────────────────────────


def test_default_registry_includes_restart_stalled_chains():
    kinds = [p.kind for p in DEFAULT_PLAYBOOKS]
    assert "restart_stalled_chains" in kinds
