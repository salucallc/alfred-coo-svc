import asyncio
from aletheia.watchers.base import BaseWatcher
from aletheia.queue import task_queue
from aletheia.verifier import verify_mesh_task


class MeshTaskSubscriber(BaseWatcher):
    """Subscriber for mesh_task_complete events."""

    async def handle(self, event: dict):
        """Process a mesh task completion event.

        Args:
            event: The event payload, expected to contain an ``id`` field.
        """
        task_id = event.get("id")
        if not task_id:
            return
        # Enqueue a verification job for the completed mesh task.
        await task_queue.enqueue(
            verify_mesh_task,
            task_id=task_id,
        )


def register():
    """Return an instance for the watcher registry."""
    return MeshTaskSubscriber()
