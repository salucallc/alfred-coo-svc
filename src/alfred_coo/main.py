"""
Main async loop for the Alfred COO daemon v0.
Handles polling mesh for tasks, claiming relevant ones, processing them via persona+dispatcher,
and maintaining health status.
"""

import asyncio
import logging

from . import config, log
from .mesh import MeshClient
from .soul import SoulClient
from .persona import get_persona
from .dispatch import Dispatcher
from .health import HealthApp

import uvicorn
from fastapi import FastAPI


logger = logging.getLogger(__name__)


async def main() -> None:
    settings = config.get_settings()
    log.setup_logging(settings.log_level, settings.log_format)

    logger.info("Starting Alfred COO daemon")

    mesh = MeshClient(base_url=settings.mesh_url, node_id=settings.node_id, session_id=settings.session_id)
    soul = SoulClient(base_url=settings.soul_url)
    dispatcher = Dispatcher()

    # Start health app
    health_app = HealthApp()
    config_uvicorn = uvicorn.Config(health_app.app, host="0.0.0.0", port=settings.health_port, log_level="error")
    server = uvicorn.Server(config_uvicorn)
    asyncio.create_task(server.serve())

    await mesh.heartbeat()

    while True:
        try:
            pending_tasks = await mesh.list_pending(limit=10)
            claimed = []

            for task in pending_tasks:
                title = task.get("title", "")
                claimable = False
                parsed_persona_name = None

                if title.startswith("[unified-plan-wave-1]"):
                    claimable = True
                    parsed_persona_name = "default"
                else:
                    parsed_persona_name = mesh.parse_persona_tag(title)
                    if parsed_persona_name:
                        claimable = True

                if not claimable:
                    continue

                success = await mesh.claim(task["id"], settings.session_id, settings.node_id)
                if success:
                    logger.info(f"Claimed task {task['id']} with persona '{parsed_persona_name}'")
                    claimed.append(task)

                    persona = get_persona(parsed_persona_name)
                    recent = await soul.recent_memories(limit=20)
                    context_str = "\n".join(m.get("content", "")[:500] for m in recent[:10])
                    system_prompt = persona.system_prompt + "\n\nRECENT CONTEXT:\n" + context_str

                    model = dispatcher.select_model(task, persona)
                    logger.debug(f"Dispatching to model {model}")

                    result = await dispatcher.call(model, system_prompt, task["description"] or task["title"])

                    await mesh.complete(
                        task["id"],
                        {
                            "content": result["content"],
                            "model": result["model_used"],
                            "tokens": {"in": result["tokens_in"], "out": result["tokens_out"]},
                        },
                    )

                    await soul.write_memory(
                        f"COO daemon completed task {task['id']}: {result['content'][:500]}",
                        topics=["coo-daemon", "task-complete"]
                    )

            health_app.mark_alive()
            await mesh.heartbeat(current_task=f"polled, claimed {len(claimed)}")
            await asyncio.sleep(settings.mesh_poll_interval_seconds)

        except Exception as e:
            logger.exception("Error in main loop: %s", e)
            health_app.mark_alive()
            await asyncio.sleep(settings.mesh_poll_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
