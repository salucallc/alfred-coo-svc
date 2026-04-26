import pytest
from aletheia.watchers.mesh_subscriber import MeshTaskSubscriber


class DummyQueue:
    def __init__(self):
        self.calls = []

    async def enqueue(self, func, *args, **kwargs):
        self.calls.append((func, args, kwargs))


@pytest.fixture
def subscriber(monkeypatch):
    dummy = DummyQueue()
    monkeypatch.setattr(
        "aletheia.watchers.mesh_subscriber.task_queue", dummy, raising=False
    )
    return MeshTaskSubscriber()


@pytest.mark.asyncio
async def test_handle_enqueues_task(subscriber):
    event = {"id": "test-id"}
    await subscriber.handle(event)
    # The subscriber should have used the dummy queue's enqueue method.
    assert len(subscriber.task_queue.calls) == 1
    func, args, kwargs = subscriber.task_queue.calls[0]
    # The verification function receives the task_id keyword argument.
    assert kwargs.get("task_id") == "test-id"
