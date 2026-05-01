"""Tests for the cockpit state rollup endpoint.

Covers:
- `list_active_orchestrators` snapshot shape against a fake orch object
- registry register / deregister lifecycle
- `/v1/cockpit/state` returns the four expected top-level keys with mocked
  soul-svc + gh shell-out
"""

from __future__ import annotations

import json
from unittest.mock import patch, AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alfred_coo import cockpit_router


class _FakeState:
    def __init__(self, current_wave=2, ticket_status=None, spend=12.34):
        self.current_wave = current_wave
        self.ticket_status = ticket_status or {}
        self.cumulative_spend_usd = spend


class _FakeGraph:
    def __init__(self, ticket_count=10):
        self.tickets = {f"t-{i}": object() for i in range(ticket_count)}


class _FakeOrch:
    def __init__(self):
        self.task = {"id": "kickoff-1", "title": "[persona:autonomous-build-a] [wave-2] kickoff"}
        self.linear_project_id = "proj-abc"
        self.state = _FakeState(
            current_wave=2,
            ticket_status={
                "u1": "DONE",
                "u2": "MERGED_GREEN",
                "u3": "DISPATCHED",
                "u4": "READY",
                "u5": "BACKED_OFF",
            },
            spend=12.34,
        )
        self.graph = _FakeGraph(ticket_count=10)


def teardown_function(_):
    # Avoid cross-test bleed in the module-level registry.
    cockpit_router._ORCH_INSTANCES.clear()
    cockpit_router._recent_merges_cache.update({"ts": 0.0, "data": []})


def test_list_active_orchestrators_shape_and_counts():
    cockpit_router.register_orchestrator("kickoff-1", _FakeOrch())
    rows = cockpit_router.list_active_orchestrators()
    assert len(rows) == 1
    row = rows[0]
    assert row["task_id"] == "kickoff-1"
    assert row["linear_project_id"] == "proj-abc"
    assert row["current_wave"] == 2
    assert row["tickets_total"] == 10
    assert row["tickets_done"] == 2  # DONE + MERGED_GREEN
    assert row["in_flight"] == 1     # DISPATCHED
    assert row["ready"] == 1         # READY
    assert row["spend_usd"] == 12.34


def test_register_deregister_lifecycle():
    cockpit_router.register_orchestrator("k1", _FakeOrch())
    cockpit_router.register_orchestrator("k2", _FakeOrch())
    assert len(cockpit_router.list_active_orchestrators()) == 2
    cockpit_router.deregister_orchestrator("k1")
    rows = cockpit_router.list_active_orchestrators()
    assert len(rows) == 1
    assert rows[0]["task_id"] == "k2"


def test_orchestrator_with_no_state_yields_zeros():
    class _Bare:
        task = {"id": "k", "title": "t"}
        linear_project_id = ""
        state = None
        graph = None

    cockpit_router.register_orchestrator("k", _Bare())
    rows = cockpit_router.list_active_orchestrators()
    assert len(rows) == 1
    assert rows[0]["tickets_total"] == 0
    assert rows[0]["current_wave"] == 0


@pytest.mark.asyncio
async def test_state_endpoint_returns_canonical_shape():
    """The `/v1/cockpit/state` route returns all four top-level keys with
    the expected nested shape, even when soul-svc + gh are unreachable."""
    cockpit_router.register_orchestrator("kickoff-1", _FakeOrch())
    app = FastAPI()
    cockpit_router.attach_cockpit(
        app,
        soul_api_url="http://unreachable.test",
        soul_api_key="dummy",
    )
    # Patch the heavy lifters so the test doesn't actually shell out or
    # hit the network. Both helpers must be patched inside the module
    # they're defined in.
    with patch.object(
        cockpit_router,
        "_fetch_mesh_sessions",
        new=AsyncMock(
            return_value=[
                {
                    "node_id": "minipc",
                    "harness": "claude-code",
                    "session_id": "alfred-main",
                    "last_heartbeat": "2026-04-29T21:00:00Z",
                }
            ]
        ),
    ), patch.object(
        cockpit_router,
        "_fetch_recent_merges",
        new=AsyncMock(
            return_value=[
                {
                    "repo": "alfred-coo-svc",
                    "pr_number": 290,
                    "title": "fix(orchestrator): skip builder dispatch when ...",
                    "merged_at": "2026-04-29T20:00:00Z",
                }
            ]
        ),
    ):
        client = TestClient(app)
        resp = client.get("/v1/cockpit/state")
        assert resp.status_code == 200
        body = resp.json()

    assert set(body.keys()) >= {
        "halt_state",
        "active_orchestrators",
        "mesh",
        "recent_merges",
        "timestamp",
    }
    assert body["halt_state"] == "dormant"
    assert len(body["active_orchestrators"]) == 1
    assert body["active_orchestrators"][0]["task_id"] == "kickoff-1"
    assert body["mesh"]["agent_count"] == 345
    assert len(body["mesh"]["active_nodes"]) == 1
    assert body["mesh"]["active_nodes"][0]["node_id"] == "minipc"
    assert len(body["recent_merges"]) == 1
    assert body["recent_merges"][0]["pr_number"] == 290
    # ≤2KB target — assert generously to avoid flakiness.
    assert len(json.dumps(body)) < 4096


@pytest.mark.asyncio
async def test_fetch_mesh_sessions_logs_exception_class_when_str_empty(
    monkeypatch, caplog,
):
    """Regression: ``mesh sessions fetch failed:`` with empty tail used to
    leak through the cockpit-router warnings stream because exceptions
    like ``httpx.ReadTimeout`` and ``CancelledError`` produce empty
    ``str(e)`` and the original log line was just ``"%s" % e``.

    The fixed line names the exception class explicitly so a future
    operator can diagnose the root cause from log output alone."""
    import logging as _logging

    class _SilentError(Exception):
        """``str(self)`` returns empty — mimics ``httpx.ReadTimeout``,
        ``httpx.PoolTimeout``, and ``asyncio.CancelledError``."""

        def __str__(self) -> str:
            return ""

    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            raise _SilentError()

    monkeypatch.setattr(
        "alfred_coo.cockpit_router.httpx.AsyncClient",
        lambda *a, **kw: _RaisingClient(),
    )
    caplog.set_level(_logging.WARNING, logger="alfred_coo.cockpit_router")

    out = await cockpit_router._fetch_mesh_sessions(
        "http://x", "k", timeout=0.1,
    )
    assert out == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("mesh sessions fetch failed" in m for m in msgs)
    assert any("_SilentError" in m for m in msgs), msgs
    assert any("<no message>" in m for m in msgs), msgs
