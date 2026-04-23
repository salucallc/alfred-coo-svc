"""Main async loop for alfred-coo-svc v0.

Polls soul-svc mesh_tasks, claims tasks with [persona:xxx] or [unified-plan-wave-1]
markers, loads recent soul memory as context, dispatches to the selected cloud
model, writes result + memory back, heartbeats, sleeps, repeats. Surviving error
handler on the loop body so one bad task never kills the daemon.

Long-running orchestrators: a persona with `handler` set opts out of the
one-shot dispatch path. After a successful claim we resolve the handler class
from `alfred_coo.autonomous_build.orchestrator` (and siblings), instantiate it,
spawn it as a detached asyncio.Task, and continue polling. See plan
Z:/_planning/v1-ga/F_autonomous_build_persona.md §1.
"""

import asyncio
import importlib
import json
import logging
from typing import Optional

import uvicorn

from . import config, log, dispatch, health
from .mesh import MeshClient, parse_persona_tag
from .soul import SoulClient
from .persona import Persona, get_persona
from .dispatch import Dispatcher
from .structured import OUTPUT_CONTRACT, parse_envelope
from .artifacts import write_artifacts
from .tools import resolve_tools, set_current_task_id, reset_current_task_id


logger = logging.getLogger(__name__)


# Module-level registry of long-running orchestrator tasks, keyed by the mesh
# task id that triggered the spawn. Exposed for tests + ops introspection.
# Entries are not cleaned up automatically in v0; on daemon restart the
# orchestrator is expected to reconcile from soul memory state (plan F §2 R2).
_running_orchestrators: dict[str, asyncio.Task] = {}


# AB-09: Layer-2 zombie guard. Maps linear_project_id -> mesh_task_id for any
# orchestrator currently running in THIS daemon. Prevents two orchestrators
# from spawning against the same Linear project when the daemon restarts and
# re-claims a kickoff whose server-side claim lease has expired.
#
# Layer 1 (soul-svc claim heartbeat / idempotent re-claim) is deferred to
# AB-11; G3 verified 2026-04-23 that soul-svc's claim endpoint returns 409
# on same-holder re-claim rather than refreshing the lease, so this local
# guard is the only defence until that server-side fix lands.
_orchestrators_by_project: dict[str, str] = {}


# Candidate modules searched by `_resolve_handler`. Order matters: first match
# wins. Kept as a list so future long-running handlers (e.g. a different
# persona class) can slot in without touching the resolver.
_HANDLER_MODULES: tuple[str, ...] = (
    "alfred_coo.autonomous_build.orchestrator",
)


def _peek_kickoff_project_id(task: dict) -> Optional[str]:
    """Best-effort extraction of `linear_project_id` from a kickoff task's
    description. Returns None if the description is missing, not JSON, or
    the key is absent. Never raises: a malformed payload must not crash the
    spawn path — it just falls back to the per-task-id tracking that was in
    place before AB-09.
    """
    desc = task.get("description") or ""
    if not desc:
        return None
    try:
        payload = json.loads(desc)
    except (ValueError, TypeError) as e:
        logger.debug(
            "kickoff description is not JSON; skipping project-id peek "
            "(task=%s, err=%s)",
            task.get("id"),
            e,
        )
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("linear_project_id")
    if pid is None:
        return None
    return str(pid)


def _resolve_handler(handler_name: str):
    """Resolve a handler class by name across the registered handler modules.

    Returns the class object or raises ImportError/AttributeError if no
    module in `_HANDLER_MODULES` defines an attribute matching `handler_name`.
    Kept sync + small so the spawn path stays trivial to test.
    """
    last_err: Exception | None = None
    for mod_name in _HANDLER_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:
            last_err = e
            continue
        cls = getattr(mod, handler_name, None)
        if cls is not None:
            return cls
        last_err = AttributeError(
            f"module {mod_name!r} has no attribute {handler_name!r}"
        )
    if last_err is None:
        last_err = ImportError(
            f"no handler module available for {handler_name!r}"
        )
    raise last_err


async def _spawn_long_running_handler(
    task: dict,
    persona: Persona,
    mesh: MeshClient,
    soul: SoulClient,
    dispatcher: Dispatcher,
    settings,
) -> bool:
    """Attempt to spawn `persona.handler` as a detached asyncio.Task.

    Returns True if the task was spawned (and stashed in
    `_running_orchestrators`), False if the handler could not be resolved
    (in which case the mesh task is marked failed here and the caller should
    skip the one-shot dispatch path).
    """
    handler_name = persona.handler or ""
    task_id = task["id"]
    try:
        cls = _resolve_handler(handler_name)
    except (ImportError, AttributeError) as e:
        logger.warning(
            "handler %s not yet implemented for task %s: %s",
            handler_name, task_id, e,
        )
        try:
            await mesh.complete(
                task_id,
                session_id=settings.soul_session_id,
                status="failed",
                result={
                    "error": (
                        f"handler {handler_name} not yet implemented; "
                        f"see AB-04"
                    ),
                },
            )
        except Exception:
            logger.exception(
                "failed to mark task %s failed after handler resolution error",
                task_id,
            )
        return False

    try:
        orch = cls(
            task=task,
            persona=persona,
            mesh=mesh,
            soul=soul,
            dispatcher=dispatcher,
            settings=settings,
        )
    except Exception:
        logger.exception(
            "handler %s instantiation failed for task %s",
            handler_name, task_id,
        )
        try:
            await mesh.complete(
                task_id,
                session_id=settings.soul_session_id,
                status="failed",
                result={
                    "error": (
                        f"handler {handler_name} instantiation raised; "
                        f"see logs"
                    ),
                },
            )
        except Exception:
            pass
        return False

    # AB-09 Layer-2 zombie guard: if we already have a running orchestrator
    # for this linear_project_id, reject the new kickoff instead of racing
    # a second one. Layer 1 (server-side claim refresh) is deferred to AB-11
    # — until that lands, this in-process check is the safety net.
    project_id = _peek_kickoff_project_id(task)
    if project_id:
        existing_task_id = _orchestrators_by_project.get(project_id)
        if existing_task_id:
            existing_task = _running_orchestrators.get(existing_task_id)
            if existing_task is not None and not existing_task.done():
                logger.warning(
                    "rejected duplicate kickoff for project=%s "
                    "(existing task=%s)",
                    project_id,
                    existing_task_id,
                )
                try:
                    await mesh.complete(
                        task_id,
                        session_id=settings.soul_session_id,
                        status="failed",
                        result={
                            "error": (
                                f"duplicate_kickoff: existing orchestrator "
                                f"task={existing_task_id} running for "
                                f"project={project_id}"
                            ),
                        },
                    )
                except Exception:
                    logger.exception(
                        "failed to mark duplicate-kickoff task %s failed",
                        task_id,
                    )
                return False
        # No live orchestrator for this project — claim the slot before we
        # create the asyncio.Task so a second concurrent spawn in the same
        # event-loop tick can't squeeze past.
        _orchestrators_by_project[project_id] = task_id

    orch_task = asyncio.create_task(
        orch.run(), name=f"orchestrator-{handler_name}-{task_id}"
    )
    _running_orchestrators[task_id] = orch_task
    if project_id:
        # Clear the project slot when the orchestrator task finishes (any
        # terminal state: success, exception, or cancellation). Scoped to
        # the specific project_id + task_id captured at spawn time so a
        # later spawn for the same project won't be clobbered.
        def _clear_project_slot(
            _t: asyncio.Task,
            pid: str = project_id,
            tid: str = task_id,
        ) -> None:
            if _orchestrators_by_project.get(pid) == tid:
                _orchestrators_by_project.pop(pid, None)

        orch_task.add_done_callback(_clear_project_slot)
    logger.info(
        "spawned long-running handler",
        extra={
            "task_id": task_id,
            "handler": handler_name,
            "persona": persona.name,
            "linear_project_id": project_id,
        },
    )
    return True


async def _run_health_server(app, port: int) -> None:
    cfg = uvicorn.Config(app, host="0.0.0.0", port=port,
                         log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)
    await server.serve()


def _fallback_urls(settings) -> list[str] | None:
    urls = list(settings.soul_api_urls or [])
    primary = settings.soul_api_url.rstrip("/")
    extras = [u.rstrip("/") for u in urls if u.rstrip("/") != primary]
    return extras or None


async def main() -> None:
    settings = config.get_settings()
    log.setup_logging(settings.log_level, settings.log_format)
    logger.info("alfred-coo v0 starting",
                extra={"session_id": settings.soul_session_id,
                       "node_id": settings.soul_node_id})

    fallback = _fallback_urls(settings)

    mesh = MeshClient(
        base_url=settings.soul_api_url,
        api_key=settings.soul_api_key,
        fallback_urls=fallback,
    )
    soul = SoulClient(
        base_url=settings.soul_api_url,
        api_key=settings.soul_api_key,
        session_id=settings.soul_session_id,
        fallback_urls=fallback,
    )
    dispatcher = Dispatcher(
        ollama_url=settings.ollama_url,
        anthropic_key=settings.anthropic_api_key,
        openrouter_key=settings.openrouter_api_key,
    )

    # Health endpoint in a background task.
    app = health.make_app()
    asyncio.create_task(_run_health_server(app, settings.health_port))
    health.mark_alive()

    # Initial heartbeat (non-fatal if it fails).
    try:
        await mesh.heartbeat(
            session_id=settings.soul_session_id,
            node_id=settings.soul_node_id,
            harness=settings.soul_harness,
            current_task="v0 daemon boot",
        )
        logger.info("initial heartbeat ok")
    except Exception as e:
        logger.warning("initial heartbeat failed (continuing): %s", e)

    while True:
        try:
            pending = await mesh.list_pending(limit=10)
            claimed = []
            for task in pending:
                title = task.get("title", "") or ""
                persona_name = parse_persona_tag(title)
                is_unified = "[unified-plan-wave-1]" in title
                if not (persona_name or is_unified):
                    continue

                # Try to claim. Claim returns the updated task record, or raises.
                try:
                    await mesh.claim(
                        task["id"], settings.soul_session_id, settings.soul_node_id
                    )
                except Exception as e:
                    logger.debug("claim skipped for %s (likely already claimed): %s",
                                 task.get("id"), e)
                    continue

                claimed.append(task["id"])
                persona = get_persona(persona_name or "default")
                logger.info("claimed task",
                            extra={"task_id": task.get("id"),
                                   "persona": persona.name,
                                   "title": title[:120]})

                # Long-running handler fork. If the persona declares a
                # handler class, spawn it as a detached asyncio.Task and
                # continue polling — main loop keeps servicing other
                # personas in parallel. Heartbeat inside the orchestrator
                # keeps this task's liveness fresh. See plan F §1.
                if persona.handler:
                    await _spawn_long_running_handler(
                        task=task,
                        persona=persona,
                        mesh=mesh,
                        soul=soul,
                        dispatcher=dispatcher,
                        settings=settings,
                    )
                    continue

                # Context load (non-fatal on error). Scoped by persona topics.
                try:
                    recent = await soul.recent_memories(
                        limit=20,
                        topics=persona.topics or None,
                    )
                except Exception as e:
                    logger.warning("recent_memories failed, continuing with empty context: %s", e)
                    recent = []

                if isinstance(recent, dict):
                    recent = recent.get("memories", [])
                context_str = "\n".join(
                    (m.get("content", "") or "")[:500]
                    for m in (recent or [])[:10]
                    if isinstance(m, dict)
                )
                system_prompt = (
                    persona.system_prompt
                    + "\n\nRECENT CONTEXT:\n"
                    + context_str
                    + OUTPUT_CONTRACT
                )

                model = dispatch.select_model(task, persona)
                tool_specs = resolve_tools(persona.tools or [])
                logger.info("dispatching",
                            extra={"task_id": task.get("id"),
                                   "model": model,
                                   "tools_enabled": len(tool_specs) > 0,
                                   "tool_count": len(tool_specs)})

                try:
                    if tool_specs:
                        ctx_token = set_current_task_id(task["id"])
                        try:
                            result = await dispatcher.call_with_tools(
                                model,
                                system_prompt,
                                task.get("description") or task.get("title", "") or "(no content)",
                                tools=tool_specs,
                                fallback_model=persona.fallback_model,
                            )
                        finally:
                            reset_current_task_id(ctx_token)
                    else:
                        result = await dispatcher.call(
                            model,
                            system_prompt,
                            task.get("description") or task.get("title", "") or "(no content)",
                            fallback_model=persona.fallback_model,
                        )
                except Exception as e:
                    logger.exception("dispatch failed for task %s", task.get("id"))
                    # Best-effort mark the task as failed so it doesn't sit claimed forever.
                    try:
                        await mesh.complete(
                            task["id"],
                            session_id=settings.soul_session_id,
                            status="failed",
                            result={
                                "error": f"dispatch failure: {type(e).__name__}: {str(e)[:500]}"
                            },
                        )
                    except Exception:
                        pass
                    continue

                raw_content = result.get("content", "") or ""
                envelope = parse_envelope(raw_content)
                artifact_paths: list[str] = []
                if envelope is not None:
                    try:
                        artifact_paths = write_artifacts(
                            task_id=task["id"],
                            artifacts=envelope.artifacts,
                        )
                    except Exception:
                        logger.exception("artifact write pass failed for task %s",
                                         task.get("id"))

                try:
                    complete_result: dict = {
                        "model": result.get("model_used"),
                        "tokens": {
                            "in": result.get("tokens_in"),
                            "out": result.get("tokens_out"),
                        },
                        "structured": envelope is not None,
                    }
                    if envelope is not None:
                        complete_result["summary"] = envelope.summary
                        complete_result["artifact_paths"] = artifact_paths
                        if envelope.follow_up_tasks:
                            complete_result["follow_up_tasks"] = envelope.follow_up_tasks
                    else:
                        # Unstructured fallback — persist full raw text so nothing is lost.
                        complete_result["content"] = raw_content
                    # Tool-use metadata (present when persona.tools is non-empty and model invoked tools)
                    if result.get("tool_calls"):
                        complete_result["tool_calls"] = result.get("tool_calls")
                        complete_result["tool_iterations"] = result.get("iterations")
                    await mesh.complete(
                        task["id"],
                        session_id=settings.soul_session_id,
                        result=complete_result,
                    )
                except Exception:
                    logger.exception("complete failed for task %s", task.get("id"))
                    continue

                # Soul memory summary entry.
                mem_summary = (
                    envelope.summary if envelope is not None
                    else raw_content[:500]
                )
                mem_topics = ["coo-daemon", "task-complete"] + list(persona.topics or [])
                try:
                    await soul.write_memory(
                        f"COO daemon completed task {task['id']} ({persona.name}): "
                        f"{mem_summary}"
                        + (f" | artifacts: {', '.join(artifact_paths)}" if artifact_paths else ""),
                        topics=mem_topics,
                    )
                except Exception as e:
                    logger.warning("soul write_memory failed (non-fatal): %s", e)

            health.mark_alive()

            try:
                await mesh.heartbeat(
                    session_id=settings.soul_session_id,
                    node_id=settings.soul_node_id,
                    harness=settings.soul_harness,
                    current_task=f"polled; claimed {len(claimed)}",
                )
            except Exception as e:
                logger.warning("periodic heartbeat failed: %s", e)

            await asyncio.sleep(settings.mesh_poll_interval_seconds)

        except Exception:
            logger.exception("unhandled error in main loop; continuing")
            health.mark_alive()
            await asyncio.sleep(settings.mesh_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
