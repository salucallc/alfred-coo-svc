"""AB-02: long-running orchestrator spawn hook tests.

Exercises `alfred_coo.main._spawn_long_running_handler` directly + the
module-level `_running_orchestrators` registry. Avoids booting the full
poll loop (which needs Supabase + Ollama creds). These tests only care
about the spawn decision: resolve handler → create asyncio.Task → stash
it, OR fail gracefully and mark the mesh task failed.
"""

import asyncio
import sys
import types

import pytest

from alfred_coo import main as main_mod
from alfred_coo.persona import Persona


# ── Test doubles ────────────────────────────────────────────────────────────


class _FakeMesh:
    """Minimal MeshClient shim. Records complete() calls for assertions."""

    def __init__(self):
        self.completions: list[dict] = []

    async def complete(self, task_id, *, session_id, status=None, result=None):
        self.completions.append(
            {
                "task_id": task_id,
                "session_id": session_id,
                "status": status,
                "result": result,
            }
        )


class _FakeSettings:
    soul_session_id = "test-session"
    soul_node_id = "test-node"
    soul_harness = "pytest"


class _FakeOrchestrator:
    """Importable handler double. `run()` is an async no-op that returns
    quickly so asyncio.Task completes without leaking."""

    instances: list["_FakeOrchestrator"] = []

    def __init__(self, *, task, persona, mesh, soul, dispatcher, settings):
        self.task = task
        self.persona = persona
        self.mesh = mesh
        self.soul = soul
        self.dispatcher = dispatcher
        self.settings = settings
        _FakeOrchestrator.instances.append(self)

    async def run(self):
        # Small yield so the Task is visibly "running" for the assertion
        # without forcing the test to wait meaningfully.
        await asyncio.sleep(0)


def _install_fake_handler_module(monkeypatch, cls, attr_name="FakeHandler"):
    """Install a fake autonomous_build.orchestrator module exposing `cls`
    under `attr_name`, and point `_HANDLER_MODULES` at it."""
    mod_name = "alfred_coo._fake_autonomous_build_orch"
    mod = types.ModuleType(mod_name)
    setattr(mod, attr_name, cls)
    monkeypatch.setitem(sys.modules, mod_name, mod)
    monkeypatch.setattr(main_mod, "_HANDLER_MODULES", (mod_name,))


def _make_persona(handler_name: str) -> Persona:
    return Persona(
        name="autonomous-build-a",
        system_prompt="test",
        preferred_model="qwen3-coder:480b-cloud",
        fallback_model="qwen3-coder:30b-a3b-q4_K_M",
        topics=["autonomous_build"],
        handler=handler_name,
    )


@pytest.fixture(autouse=True)
def _clear_orchestrator_registry():
    """Each test starts with a clean `_running_orchestrators`,
    `_orchestrators_by_project`, and FakeHandler instance list. Prevents
    cross-test leakage since the registries are module-level dicts."""
    main_mod._running_orchestrators.clear()
    # AB-09: also reset the project-slot registry.
    main_mod._orchestrators_by_project.clear()
    _FakeOrchestrator.instances.clear()
    yield
    # Cancel any lingering tasks so pytest doesn't warn about them.
    for t in list(main_mod._running_orchestrators.values()):
        if not t.done():
            t.cancel()
    main_mod._running_orchestrators.clear()
    main_mod._orchestrators_by_project.clear()


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_spawn_creates_asyncio_task_and_stashes_it(monkeypatch):
    """AB-02 (a): when the handler import succeeds, the spawn helper must
    create an asyncio.Task and stash it in `_running_orchestrators` keyed
    by mesh task id. It must NOT await the orchestrator — the main loop
    is expected to keep polling."""
    _install_fake_handler_module(monkeypatch, _FakeOrchestrator)
    mesh = _FakeMesh()
    task = {"id": "mesh-task-123", "title": "[persona:autonomous-build-a] kickoff"}
    persona = _make_persona("FakeHandler")

    spawned = await main_mod._spawn_long_running_handler(
        task=task,
        persona=persona,
        mesh=mesh,
        soul=object(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )

    assert spawned is True
    assert "mesh-task-123" in main_mod._running_orchestrators
    stashed = main_mod._running_orchestrators["mesh-task-123"]
    assert isinstance(stashed, asyncio.Task)
    # Orchestrator was instantiated with the expected task.
    assert _FakeOrchestrator.instances, "orchestrator was not instantiated"
    assert _FakeOrchestrator.instances[0].task is task
    # No failure completion — we should NOT have marked the task failed.
    assert mesh.completions == []
    # Let the fake run() finish so pytest doesn't warn.
    await stashed


async def test_spawn_marks_task_failed_on_missing_handler(monkeypatch):
    """AB-02 (b): when the handler class cannot be resolved (AB-04 not yet
    landed), the spawn helper must mark the mesh task failed with a clear
    pointer to AB-04, and MUST NOT stash anything in the registry."""
    # Point _HANDLER_MODULES at a module that exists but doesn't export the
    # handler class — exercises the AttributeError branch.
    mod_name = "alfred_coo._fake_empty_handler_module"
    mod = types.ModuleType(mod_name)  # no AutonomousBuildOrchestrator attr
    monkeypatch.setitem(sys.modules, mod_name, mod)
    monkeypatch.setattr(main_mod, "_HANDLER_MODULES", (mod_name,))

    mesh = _FakeMesh()
    task = {"id": "mesh-task-456", "title": "[persona:autonomous-build-a] kickoff"}
    persona = _make_persona("AutonomousBuildOrchestrator")

    spawned = await main_mod._spawn_long_running_handler(
        task=task,
        persona=persona,
        mesh=mesh,
        soul=object(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )

    assert spawned is False
    assert "mesh-task-456" not in main_mod._running_orchestrators
    assert len(mesh.completions) == 1
    done = mesh.completions[0]
    assert done["task_id"] == "mesh-task-456"
    assert done["status"] == "failed"
    assert "AutonomousBuildOrchestrator" in done["result"]["error"]
    assert "AB-04" in done["result"]["error"]


async def test_spawn_marks_task_failed_on_import_error(monkeypatch):
    """AB-02 (b, ImportError branch): if the handler module itself cannot be
    imported (the AB-04 package not yet present), the helper catches
    ImportError and marks the mesh task failed."""
    # Point at a module that does not exist; _resolve_handler should hit
    # ImportError on import_module.
    monkeypatch.setattr(
        main_mod,
        "_HANDLER_MODULES",
        ("alfred_coo._does_not_exist_anywhere",),
    )
    # Ensure it is truly absent from sys.modules.
    sys.modules.pop("alfred_coo._does_not_exist_anywhere", None)

    mesh = _FakeMesh()
    task = {"id": "mesh-task-789", "title": "[persona:autonomous-build-a] kickoff"}
    persona = _make_persona("AutonomousBuildOrchestrator")

    spawned = await main_mod._spawn_long_running_handler(
        task=task,
        persona=persona,
        mesh=mesh,
        soul=object(),
        dispatcher=object(),
        settings=_FakeSettings(),
    )

    assert spawned is False
    assert "mesh-task-789" not in main_mod._running_orchestrators
    assert len(mesh.completions) == 1
    assert mesh.completions[0]["status"] == "failed"
    assert "AB-04" in mesh.completions[0]["result"]["error"]


async def test_spawn_is_non_blocking(monkeypatch):
    """AB-02 (c): spawning a handler must NOT block the caller on
    orchestrator.run() completion. We prove this by using a handler whose
    run() never completes, calling the spawn helper under a short
    asyncio.wait_for, and asserting the helper returns promptly while the
    orchestrator task is still pending."""

    class _NeverEndingOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            # Sleep longer than the wait_for timeout on purpose.
            await asyncio.sleep(60)

    _install_fake_handler_module(
        monkeypatch, _NeverEndingOrchestrator, attr_name="NeverEnding"
    )

    mesh = _FakeMesh()
    task = {"id": "mesh-task-nb", "title": "[persona:autonomous-build-a] kickoff"}
    persona = _make_persona("NeverEnding")

    spawned = await asyncio.wait_for(
        main_mod._spawn_long_running_handler(
            task=task,
            persona=persona,
            mesh=mesh,
            soul=object(),
            dispatcher=object(),
            settings=_FakeSettings(),
        ),
        timeout=2.0,
    )

    assert spawned is True
    assert "mesh-task-nb" in main_mod._running_orchestrators
    stashed = main_mod._running_orchestrators["mesh-task-nb"]
    assert not stashed.done(), (
        "orchestrator task completed synchronously; main loop would have "
        "been blocked on it"
    )
    # Fixture teardown cancels the lingering task.


async def test_resolve_handler_prefers_first_module_match(monkeypatch):
    """AB-04 handoff contract: `_resolve_handler` walks `_HANDLER_MODULES`
    in order and returns the first module that exports the requested class.
    Exercises the resolver in isolation to nail down the contract AB-04
    will land against."""

    class Target:
        pass

    mod_a_name = "alfred_coo._fake_handler_module_a"
    mod_b_name = "alfred_coo._fake_handler_module_b"
    mod_a = types.ModuleType(mod_a_name)  # does NOT export Target
    mod_b = types.ModuleType(mod_b_name)
    mod_b.Target = Target  # second module wins

    monkeypatch.setitem(sys.modules, mod_a_name, mod_a)
    monkeypatch.setitem(sys.modules, mod_b_name, mod_b)
    monkeypatch.setattr(main_mod, "_HANDLER_MODULES", (mod_a_name, mod_b_name))

    cls = main_mod._resolve_handler("Target")
    assert cls is Target


# ── AB-09: zombie orchestrator spawn guard (Layer 2) ────────────────────────


def _kickoff_task(task_id: str, project_id: str | None, title: str = "kickoff") -> dict:
    """Build a kickoff mesh task with a JSON description carrying the
    linear_project_id (or no description if project_id is None)."""
    if project_id is None:
        desc = ""
    else:
        import json as _json
        desc = _json.dumps({"linear_project_id": project_id})
    return {
        "id": task_id,
        "title": f"[persona:autonomous-build-a] {title}",
        "description": desc,
    }


async def test_duplicate_kickoff_same_project_fails_new(monkeypatch):
    """AB-09: a second kickoff for the same linear_project_id while the
    first orchestrator is still running must be rejected with a
    `duplicate_kickoff` error, and `_running_orchestrators` must still
    only contain the original task."""

    class _NeverEndingOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            await asyncio.sleep(60)

    _install_fake_handler_module(
        monkeypatch, _NeverEndingOrchestrator, attr_name="NeverEnding"
    )
    mesh = _FakeMesh()
    persona = _make_persona("NeverEnding")

    # First kickoff spawns normally.
    t1 = _kickoff_task("T1", "P")
    spawned1 = await main_mod._spawn_long_running_handler(
        task=t1, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    )
    assert spawned1 is True
    assert "T1" in main_mod._running_orchestrators
    assert main_mod._orchestrators_by_project["P"] == "T1"
    assert mesh.completions == []

    # Second kickoff for the same project must be rejected.
    t2 = _kickoff_task("T2", "P")
    spawned2 = await main_mod._spawn_long_running_handler(
        task=t2, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    )

    assert spawned2 is False
    # T2 must NOT have been stashed — only T1 should be in the registry.
    assert "T2" not in main_mod._running_orchestrators
    assert list(main_mod._running_orchestrators.keys()) == ["T1"]
    # Project slot still owned by T1.
    assert main_mod._orchestrators_by_project["P"] == "T1"
    # mesh.complete was called exactly once, marking T2 failed with the
    # duplicate_kickoff error.
    assert len(mesh.completions) == 1
    dup = mesh.completions[0]
    assert dup["task_id"] == "T2"
    assert dup["status"] == "failed"
    assert "duplicate_kickoff" in dup["result"]["error"]
    assert "T1" in dup["result"]["error"]
    assert "P" in dup["result"]["error"]


async def test_different_projects_both_spawn(monkeypatch):
    """AB-09: two kickoffs for two different projects must both spawn —
    the guard is scoped per linear_project_id, not global."""

    class _NeverEndingOrchestrator:
        def __init__(self, **_kwargs):
            pass

        async def run(self):
            await asyncio.sleep(60)

    _install_fake_handler_module(
        monkeypatch, _NeverEndingOrchestrator, attr_name="NeverEnding"
    )
    mesh = _FakeMesh()
    persona = _make_persona("NeverEnding")

    t1 = _kickoff_task("T1", "P1")
    t2 = _kickoff_task("T2", "P2")

    assert await main_mod._spawn_long_running_handler(
        task=t1, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    ) is True
    assert await main_mod._spawn_long_running_handler(
        task=t2, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    ) is True

    assert "T1" in main_mod._running_orchestrators
    assert "T2" in main_mod._running_orchestrators
    assert main_mod._orchestrators_by_project == {"P1": "T1", "P2": "T2"}
    assert mesh.completions == []


async def test_completion_clears_project_slot(monkeypatch):
    """AB-09: when an orchestrator task finishes (any terminal state), the
    done_callback must clear the project slot so a subsequent kickoff for
    the same project is allowed to spawn."""
    _install_fake_handler_module(monkeypatch, _FakeOrchestrator)
    mesh = _FakeMesh()
    persona = _make_persona("FakeHandler")

    t1 = _kickoff_task("T1", "P")
    assert await main_mod._spawn_long_running_handler(
        task=t1, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    ) is True
    assert main_mod._orchestrators_by_project["P"] == "T1"

    # _FakeOrchestrator.run() is `await asyncio.sleep(0)` — it completes
    # almost immediately. Await the task to drive it to done, then yield
    # once more so the done_callback fires.
    stashed = main_mod._running_orchestrators["T1"]
    await stashed
    # Yield to let add_done_callback scheduling flush.
    await asyncio.sleep(0)

    assert main_mod._orchestrators_by_project.get("P") is None

    # A fresh kickoff for the same project should now spawn cleanly.
    t2 = _kickoff_task("T2", "P")
    spawned2 = await main_mod._spawn_long_running_handler(
        task=t2, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    )
    assert spawned2 is True
    assert main_mod._orchestrators_by_project["P"] == "T2"
    # No duplicate error completions.
    assert mesh.completions == []
    await main_mod._running_orchestrators["T2"]


async def test_payload_without_project_id_still_spawns(monkeypatch):
    """AB-09: a kickoff whose description has no linear_project_id (e.g. a
    literal empty-object JSON payload) must still spawn — the guard only
    activates when we can identify the project. Falls back to the pre-AB-09
    per-task-id tracking."""
    _install_fake_handler_module(monkeypatch, _FakeOrchestrator)
    mesh = _FakeMesh()
    persona = _make_persona("FakeHandler")

    task = {
        "id": "T-no-pid",
        "title": "[persona:autonomous-build-a] kickoff",
        "description": "{}",  # valid JSON, but no linear_project_id key
    }

    spawned = await main_mod._spawn_long_running_handler(
        task=task, persona=persona, mesh=mesh,
        soul=object(), dispatcher=object(), settings=_FakeSettings(),
    )
    assert spawned is True
    assert "T-no-pid" in main_mod._running_orchestrators
    # No duplicate_kickoff completion was emitted.
    assert not any(
        (c.get("result") or {}).get("error", "").startswith("duplicate_kickoff")
        for c in mesh.completions
    )
    # Project registry stays empty because no id was extractable.
    assert main_mod._orchestrators_by_project == {}
    await main_mod._running_orchestrators["T-no-pid"]


def test_peek_kickoff_project_id_handles_non_json_desc():
    """AB-09: `_peek_kickoff_project_id` must never raise on a malformed
    description (markdown, plain text, truncated JSON, etc.). It returns
    None so the spawn path falls back to pre-AB-09 behavior."""
    # Markdown-ish prose, not JSON.
    assert main_mod._peek_kickoff_project_id(
        {"id": "X", "description": "# Kickoff\n\nHello there."}
    ) is None
    # Empty description.
    assert main_mod._peek_kickoff_project_id(
        {"id": "X", "description": ""}
    ) is None
    # Missing description key entirely.
    assert main_mod._peek_kickoff_project_id({"id": "X"}) is None
    # JSON array at top level (not a dict) — should not explode.
    assert main_mod._peek_kickoff_project_id(
        {"id": "X", "description": '["not", "a", "dict"]'}
    ) is None
    # Truncated / invalid JSON.
    assert main_mod._peek_kickoff_project_id(
        {"id": "X", "description": '{"linear_project_id": "P"'}
    ) is None
    # Happy path: valid JSON with the key.
    assert main_mod._peek_kickoff_project_id(
        {"id": "X", "description": '{"linear_project_id": "P-OK"}'}
    ) == "P-OK"
