"""Substrate task #80 (2026-05-02): boot-time orphan recovery tests.

A daemon restart leaves any in-flight task claimed by ``alfred-coo``
session_id but with no living process to advance it. ``_recover_orphaned_tasks``
scans claimed tasks at startup and marks every orphan as failed so the
chain can resume. Tonight (2026-05-02 06:30Z + 07:21Z + 07:33Z) cleaned
8 such orphans by hand across three restarts — clear evidence automation
is overdue.
"""

from __future__ import annotations

from typing import Any

import pytest

from alfred_coo.orphan_recovery import recover_orphaned_tasks as _recover_orphaned_tasks


SESSION_ID = "alfred-coo"


class _FakeMesh:
    """MeshClient stub. Records list_tasks + complete calls."""

    def __init__(self, claimed: list[dict] | None = None,
                 list_raises: bool = False, complete_raises_on: set[str] | None = None):
        self._claimed = claimed or []
        self._list_raises = list_raises
        self._complete_raises_on = complete_raises_on or set()
        self.list_calls: list[dict] = []
        self.completions: list[dict] = []

    async def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        self.list_calls.append({"status": status, "limit": limit})
        if self._list_raises:
            raise RuntimeError("simulated mesh outage")
        if status == "claimed":
            return list(self._claimed)
        return []

    async def complete(
        self, task_id: str, *, session_id: str,
        status: str = "completed", result: dict | None = None,
    ) -> dict:
        rec = {
            "task_id": task_id,
            "session_id": session_id,
            "status": status,
            "result": result or {},
        }
        self.completions.append(rec)
        if task_id in self._complete_raises_on:
            raise RuntimeError(f"simulated complete failure for {task_id}")
        return {"id": task_id, "status": status}


def _orphan(task_id: str, session_id: str = SESSION_ID, title: str = "test task") -> dict:
    return {
        "id": task_id,
        "title": title,
        "assigned_session_id": session_id,
        "status": "claimed",
    }


@pytest.mark.asyncio
async def test_recover_orphans_marks_them_failed():
    """The canonical case: 3 tasks orphaned by THIS session_id at startup
    are all marked failed. Recovery count returned for log/metric emit."""
    mesh = _FakeMesh(claimed=[
        _orphan("task-a", title="[persona:autonomous-build-a] kickoff X"),
        _orphan("task-b", title="[persona:alfred-coo-a] SAL-1234 builder"),
        _orphan("task-c", title="[persona:alfred-doctor] surveillance tick"),
    ])
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 3
    assert len(mesh.completions) == 3
    for rec in mesh.completions:
        assert rec["status"] == "failed"
        assert rec["session_id"] == SESSION_ID
        assert rec["result"]["reason"] == "orphaned_by_daemon_restart"
        assert "title_excerpt" in rec["result"]
        assert "recovered_at" in rec["result"]


@pytest.mark.asyncio
async def test_recover_orphans_skips_other_sessions():
    """Tasks claimed by OTHER sessions (e.g. tiresias-mssp-pdp, soul-svc-mcp,
    or a sibling alfred instance on another node) are NOT touched. Recovery
    is scoped to ``session_id`` exact match."""
    mesh = _FakeMesh(claimed=[
        _orphan("ours-1", session_id=SESSION_ID, title="ours"),
        _orphan("theirs-1", session_id="tiresias-mssp-pdp", title="not ours"),
        _orphan("theirs-2", session_id="soul-svc-mcp", title="also not"),
    ])
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 1
    assert len(mesh.completions) == 1
    assert mesh.completions[0]["task_id"] == "ours-1"


@pytest.mark.asyncio
async def test_recover_orphans_handles_no_orphans():
    """Clean shutdown / fresh boot — no claimed tasks to recover. Function
    returns 0, makes one list_tasks call, no completions."""
    mesh = _FakeMesh(claimed=[])
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 0
    assert len(mesh.completions) == 0
    # Sanity: list was still called (single GET RTT cost).
    assert len(mesh.list_calls) == 1
    assert mesh.list_calls[0]["status"] == "claimed"


@pytest.mark.asyncio
async def test_recover_orphans_swallows_list_failure():
    """If the mesh list_tasks RPC fails (mesh down at boot), recovery
    returns 0 and DOES NOT raise — startup must not be blocked."""
    mesh = _FakeMesh(list_raises=True)
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 0
    assert len(mesh.completions) == 0


@pytest.mark.asyncio
async def test_recover_orphans_continues_past_individual_complete_failures():
    """If complete() fails on one task (e.g. soul-svc race-condition where
    another session beat us to the patch), recovery moves on to the next.
    Returns count of successes only."""
    mesh = _FakeMesh(
        claimed=[
            _orphan("a", title="ok 1"),
            _orphan("b", title="will-fail"),
            _orphan("c", title="ok 2"),
        ],
        complete_raises_on={"b"},
    )
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 2  # a + c succeeded; b raised but didn't block c
    completed_ids = [r["task_id"] for r in mesh.completions if r["task_id"] != "b" or False]
    # Order: all three were attempted; b's attempt was recorded then raised.
    assert {r["task_id"] for r in mesh.completions} == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_recover_orphans_skips_tasks_without_id():
    """Defensive: if a task record from soul-svc lacks an id field (data
    corruption), skip it rather than crash."""
    mesh = _FakeMesh(claimed=[
        {"id": "good-task", "assigned_session_id": SESSION_ID, "title": "ok"},
        {"id": None, "assigned_session_id": SESSION_ID, "title": "no id"},
        {"id": "", "assigned_session_id": SESSION_ID, "title": "empty id"},
    ])
    n = await _recover_orphaned_tasks(mesh, session_id=SESSION_ID)
    assert n == 1
    assert mesh.completions[0]["task_id"] == "good-task"
