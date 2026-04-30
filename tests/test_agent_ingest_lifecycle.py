import asyncio
import pytest
from src.alfred_coo.agent_ingest.lifecycle_scheduler import start_scheduler

class DummyPlugin:
    def __init__(self):
        self.calls = 0
    def lifecycle(self, phase, agent_id):
        if phase == "health":
            self.calls += 1
            return {"status": "ok"}

@pytest.mark.asyncio
async def test_lifecycle_scheduler_runs_at_least_once():
    plugin = DummyPlugin()
    task = start_scheduler(plugin, agent_id="a1", cadence=0.1)
    await asyncio.sleep(0.25)
    task.cancel()
    assert plugin.calls >= 2
