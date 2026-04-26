import json
import asyncio
from typing import Any, Dict

# Assuming a Redis client abstraction is available in the service
# In production this would be injected; here we import a placeholder.

try:
    from aletheia.infrastructure.redis_client import redis_client
except ImportError:
    # Simple fallback for testing environment
    import redis
    redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

QUEUE_NAME = "aletheia:pending"


def _enqueue_job(event: Dict[str, Any]) -> None:
    """Push a mesh task complete event onto the verification queue.

    The event payload is serialized as JSON; the worker loop will later
    deserialize and handle verification.
    """
    payload = json.dumps(event)
    redis_client.rpush(QUEUE_NAME, payload)


async def handle_mesh_task_complete(event: Dict[str, Any]) -> None:
    """Entry point for the subscriber.

    Expected `event` shape (minimal example)::

        {
            "task_id": "<uuid>",
            "timestamp": "2026-04-26T19:00:00Z",
            "metadata": {...}
        }

    The function validates the presence of ``task_id`` and enqueues the job.
    """
    if not isinstance(event, dict) or "task_id" not in event:
        raise ValueError("Invalid mesh task complete event payload")
    # In a real deployment additional validation could be added.
    _enqueue_job(event)
    # Returning None signals successful handling.
    return None

# For graceful shutdown in the service process
async def start_watcher() -> None:
    """Placeholder coroutine to illustrate subscription setup.

    In the actual service this would subscribe to the Redis pub/sub channel
    ``"mesh_task_complete"`` and call ``handle_mesh_task_complete`` for each
    message.
    """
    pubsub = redis_client.pubsub()
    await asyncio.get_event_loop().run_in_executor(None, pubsub.subscribe, "mesh_task_complete")
    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            event = json.loads(message["data"])
            await handle_mesh_task_complete(event)
        except Exception as exc:
            # Log and continue; failure to process a single event should not crash the watcher.
            print(f"[mesh_subscriber] failed to process event: {exc}")

# When this module is imported by the main service, ``start_watcher`` will be scheduled.
