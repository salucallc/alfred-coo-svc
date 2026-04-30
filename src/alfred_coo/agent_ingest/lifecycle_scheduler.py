import asyncio
from typing import Any

DEFAULT_CADENCE_SECONDS = 30

async def _run_health_check(plugin: Any, agent_id: str):
    """Call plugin.lifecycle('health') and pretend to update DB."""
    try:
        result = plugin.lifecycle("health", agent_id)
    except Exception:
        result = None
    return result

async def _scheduler(plugin: Any, agent_id: str, cadence: int):
    while True:
        await _run_health_check(plugin, agent_id)
        await asyncio.sleep(cadence)

def start_scheduler(plugin: Any, agent_id: str, cadence: int = DEFAULT_CADENCE_SECONDS) -> asyncio.Task:
    """
    Launch the health‑check scheduler as an asyncio task.

    Returns the created Task so the caller can cancel it if needed.
    """
    loop = asyncio.get_event_loop()
    task = loop.create_task(_scheduler(plugin, agent_id, cadence))
    return task
