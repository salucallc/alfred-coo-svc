"""Main async loop for alfred-coo-svc v0.

Polls soul-svc mesh_tasks, claims tasks with [persona:xxx] or [unified-plan-wave-1]
markers, loads recent soul memory as context, dispatches to the selected cloud
model, writes result + memory back, heartbeats, sleeps, repeats. Surviving error
handler on the loop body so one bad task never kills the daemon.
"""

import asyncio
import logging

import uvicorn

from . import config, log, dispatch, health
from .mesh import MeshClient, parse_persona_tag
from .soul import SoulClient
from .persona import get_persona
from .dispatch import Dispatcher
from .structured import OUTPUT_CONTRACT, parse_envelope
from .artifacts import write_artifacts
from .tools import resolve_tools, set_current_task_id, reset_current_task_id


logger = logging.getLogger(__name__)


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
