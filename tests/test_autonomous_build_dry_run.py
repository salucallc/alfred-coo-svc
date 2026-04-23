"""AB-07 unit tests for DryRunAdapter + orchestrator dry-run wiring.

Covers the adapter surface in isolation (no orchestrator) plus the
`maybe_apply_dry_run` wiring that detects the env var and swaps clients
on an orchestrator instance.

The heavy end-to-end smoke test lives in
`tests/smoke/test_autonomous_build_smoke.py` and is gated by the
`smoke` pytest marker so regular runs stay fast.
"""

from __future__ import annotations

import asyncio
import json


from alfred_coo.autonomous_build.dry_run import (
    DEFAULT_DRY_RUN_RESULT,
    DryRunAdapter,
    DryRunMesh,
    apply_dry_run,
    dry_run_enabled,
    maybe_apply_dry_run,
)
from alfred_coo.autonomous_build.orchestrator import (
    AutonomousBuildOrchestrator,
)


# -- Minimal fakes (mirror test_autonomous_build_orchestrator.py) -----------


class _FakeMesh:
    async def create_task(self, *, title, description="", from_session_id=None):
        return {"id": "real-1", "title": title, "status": "pending"}

    async def list_tasks(self, status=None, limit=50):
        return []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        return {"id": task_id, "status": status}


class _FakeSoul:
    async def write_memory(self, content, topics=None):
        return {"memory_id": "m-1"}

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


def _mk_orchestrator(kickoff_desc=None, mesh=None, soul=None):
    if isinstance(kickoff_desc, dict):
        kickoff_desc = json.dumps(kickoff_desc)
    task = {
        "id": "kick-dryrun",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": kickoff_desc or "",
    }
    return AutonomousBuildOrchestrator(
        task=task,
        persona=_mk_persona(),
        mesh=mesh or _FakeMesh(),
        soul=soul or _FakeSoul(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )


# -- DryRunAdapter unit tests ---------------------------------------------


async def test_dryrun_adapter_creates_fake_tasks():
    """create_task returns a dryrun-<n> id, increments the counter, and
    stashes the task for later list_tasks retrieval."""
    adapter = DryRunAdapter(auto_complete_after_seconds=0.0)

    rec_a = await adapter.create_task(title="t-A", description="desc-a")
    rec_b = await adapter.create_task(title="t-B", description="desc-b",
                                      from_session_id="sess-1")

    assert rec_a["id"] == "dryrun-1"
    assert rec_b["id"] == "dryrun-2"
    assert rec_a["title"] == "t-A"
    assert rec_b["from_session_id"] == "sess-1"
    assert adapter._counter == 2

    # list_tasks reflects both.
    all_tasks = await adapter.list_tasks()
    assert {t["id"] for t in all_tasks} == {"dryrun-1", "dryrun-2"}


async def test_dryrun_adapter_auto_completes_after_delay():
    """Tasks younger than auto_complete_after_seconds stay pending;
    older ones materialise as completed with the default result."""
    adapter = DryRunAdapter(auto_complete_after_seconds=0.1)

    await adapter.create_task(title="young")
    pending = await adapter.list_tasks(status="pending")
    assert len(pending) == 1, pending
    assert pending[0]["id"] == "dryrun-1"

    completed_before = await adapter.list_tasks(status="completed")
    assert completed_before == []

    # Wait past the threshold.
    await asyncio.sleep(0.15)

    completed = await adapter.list_tasks(status="completed")
    assert len(completed) == 1
    assert completed[0]["id"] == "dryrun-1"
    assert completed[0]["result"]["summary"] == DEFAULT_DRY_RUN_RESULT["summary"]
    assert completed[0]["result"]["tokens"]["in"] == 100
    assert completed[0]["result"]["tokens"]["out"] == 50
    assert completed[0]["result"]["model"] == "qwen3-coder:480b-cloud"


async def test_dryrun_adapter_scripted_result_overrides_default():
    """set_scripted_result + script_next replace the default result for
    a specific task id."""
    adapter = DryRunAdapter(auto_complete_after_seconds=0.0)

    await adapter.create_task(title="a")
    await adapter.create_task(title="b")

    adapter.set_scripted_result(
        "dryrun-1",
        {"summary": "scripted-A", "tokens": {"in": 10, "out": 5},
         "model": "gpt-oss:20b-cloud"},
    )
    # script_next targets the most recent (dryrun-2)
    adapter.script_next(
        {"summary": "scripted-B", "tokens": {"in": 999, "out": 1},
         "model": "deepseek-v3.2:cloud"},
    )

    completed = await adapter.list_tasks(status="completed")
    by_id = {t["id"]: t for t in completed}
    assert by_id["dryrun-1"]["result"]["summary"] == "scripted-A"
    assert by_id["dryrun-1"]["result"]["model"] == "gpt-oss:20b-cloud"
    assert by_id["dryrun-2"]["result"]["summary"] == "scripted-B"
    assert by_id["dryrun-2"]["result"]["tokens"]["in"] == 999


async def test_dryrun_slack_post_prints_prefix(capsys):
    """slack_post writes the `[DRY-RUN slack]` prefixed line to stdout
    and records the message in the adapter's post log."""
    adapter = DryRunAdapter()
    resp = await adapter.slack_post(message="hello world", channel="C123")

    assert resp["channel"] == "C123"
    assert resp["ts"]  # non-empty
    assert len(adapter.slack_posts) == 1
    assert adapter.slack_posts[0]["message"] == "hello world"
    assert adapter.slack_posts[0]["channel"] == "C123"

    captured = capsys.readouterr()
    assert "[DRY-RUN slack]" in captured.out
    assert "hello world" in captured.out
    assert "C123" in captured.out


async def test_dryrun_slack_ack_poll_auto_acks():
    """slack_ack_poll returns {matched: True, matched_keyword} immediately
    and logs the call for assertion."""
    adapter = DryRunAdapter()
    resp = await adapter.slack_ack_poll(
        channel="C0ASAKFTR1C",
        after_ts="12345.6789",
        author_user_id="U1",
        keywords=["ACK SS-08", "approved"],
    )
    assert resp["matched"] is True
    assert resp["message_ts"] == "1"
    assert resp["matched_keyword"] == "ACK SS-08"
    assert len(adapter.ack_polls) == 1
    rec = adapter.ack_polls[0]
    assert rec["channel"] == "C0ASAKFTR1C"
    assert rec["after_ts"] == "12345.6789"
    assert rec["author_user_id"] == "U1"
    assert rec["keywords"] == ["ACK SS-08", "approved"]


async def test_dryrun_mesh_shim_routes_to_adapter():
    """DryRunMesh must forward create_task/list_tasks/complete to the
    wrapped adapter without re-implementing behaviour."""
    adapter = DryRunAdapter(auto_complete_after_seconds=0.0)
    mesh = DryRunMesh(adapter)

    rec = await mesh.create_task(
        title="via-shim", description="desc", from_session_id="sess",
    )
    assert rec["id"] == "dryrun-1"

    listed = await mesh.list_tasks(status="completed")
    assert len(listed) == 1

    comp = await mesh.complete(
        task_id="dryrun-1",
        session_id="sess",
        result={"summary": "done"},
        status="completed",
    )
    assert comp["id"] == "dryrun-1"
    assert comp["status"] == "completed"
    assert len(adapter.completions) == 1


async def test_dryrun_adapter_linear_update_logs_only():
    adapter = DryRunAdapter()
    resp = await adapter.linear_update_issue_state(
        issue_id="SAL-2684", state_name="In Progress",
    )
    assert resp["ok"] is True
    assert resp["identifier"] == "SAL-2684"
    assert len(adapter.linear_updates) == 1
    assert adapter.linear_updates[0] == {
        "issue_id": "SAL-2684", "state_name": "In Progress",
    }


# -- env-var detection -----------------------------------------------------


def test_dry_run_enabled_reads_truthy_values(monkeypatch):
    for truthy in ("1", "true", "TRUE", "Yes", "on"):
        monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", truthy)
        assert dry_run_enabled() is True, f"{truthy!r} should be truthy"

    for falsy in ("", "0", "false", "no", "off", "garbage"):
        monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", falsy)
        assert dry_run_enabled() is False, f"{falsy!r} should be falsy"


# -- orchestrator wiring ---------------------------------------------------


def test_orchestrator_dry_run_env_detected_and_swaps_clients(monkeypatch):
    """With the env var set, instantiating the orchestrator must swap
    mesh / cadence slack_post / ack-poll resolver / linear update."""
    monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", "1")

    fake_mesh = _FakeMesh()
    orch = _mk_orchestrator(mesh=fake_mesh)

    # Mesh is now a DryRunMesh shim, NOT the _FakeMesh we passed in.
    assert isinstance(orch.mesh, DryRunMesh)
    # Adapter is discoverable on the instance.
    assert isinstance(orch._dry_run_adapter, DryRunAdapter)

    # Cadence slack_post_fn points at the adapter's slack_post. Bound-method
    # identity is unstable across attribute access, so compare the underlying
    # function + __self__.
    adapter = orch._dry_run_adapter
    assert orch.cadence._slack_post_fn.__func__ is adapter.slack_post.__func__
    assert orch.cadence._slack_post_fn.__self__ is adapter

    # ack-poll resolver returns the adapter's auto-ACK.
    resolved = orch._resolve_slack_ack_poll()
    assert resolved.__func__ is adapter.slack_ack_poll.__func__
    assert resolved.__self__ is adapter


def test_orchestrator_dry_run_not_applied_when_env_unset(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_BUILD_DRY_RUN", raising=False)
    orch = _mk_orchestrator()
    assert orch._dry_run_adapter is None
    # Mesh is whatever we passed (our _FakeMesh here).
    assert isinstance(orch.mesh, _FakeMesh)


async def test_apply_dry_run_is_idempotent(monkeypatch):
    """apply_dry_run called twice just re-binds — no compounding state."""
    orch = _mk_orchestrator()
    first = apply_dry_run(orch)
    assert isinstance(first, DryRunAdapter)
    first_mesh = orch.mesh

    second = apply_dry_run(orch)
    assert second is not first
    # New adapter, but mesh still a DryRunMesh pointing at the new one.
    assert isinstance(orch.mesh, DryRunMesh)
    assert orch.mesh is not first_mesh
    assert orch._dry_run_adapter is second

    # Exercise through the shim to confirm binding.
    rec = await orch.mesh.create_task(title="post-rebind")
    assert rec["id"] == "dryrun-1"  # fresh adapter counter


async def test_orchestrator_update_linear_state_routes_through_adapter(
    monkeypatch,
):
    """After dry-run wiring, _update_linear_state should append to the
    adapter's linear_updates log, not reach out to BUILTIN_TOOLS."""
    monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", "1")
    orch = _mk_orchestrator()

    class _Ticket:
        id = "uuid-1"
        identifier = "SAL-2684"

    await orch._update_linear_state(_Ticket(), "In Progress")
    adapter = orch._dry_run_adapter
    assert len(adapter.linear_updates) == 1
    assert adapter.linear_updates[0]["issue_id"] == "uuid-1"
    assert adapter.linear_updates[0]["state_name"] == "In Progress"


async def test_parse_payload_rebuild_keeps_cadence_bound_to_adapter(
    monkeypatch,
):
    """_parse_payload rebuilds self.cadence from the payload; the hook
    must re-bind the adapter's slack_post_fn onto the new cadence so
    status posts keep going through the dry-run stdout stub."""
    monkeypatch.setenv("AUTONOMOUS_BUILD_DRY_RUN", "1")
    payload = {
        "linear_project_id": "proj-x",
        "status_cadence": {"interval_minutes": 5,
                           "slack_channel": "C-TEST"},
    }
    orch = _mk_orchestrator(kickoff_desc=payload)
    # Simulate run() parsing the payload.
    orch._parse_payload()

    adapter = orch._dry_run_adapter
    assert orch.cadence.channel == "C-TEST"
    assert orch.cadence._slack_post_fn.__func__ is adapter.slack_post.__func__
    assert orch.cadence._slack_post_fn.__self__ is adapter


def test_maybe_apply_dry_run_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_BUILD_DRY_RUN", raising=False)

    class _Dummy:
        pass

    assert maybe_apply_dry_run(_Dummy()) is None
