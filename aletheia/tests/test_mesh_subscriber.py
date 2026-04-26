import json
import pytest
from unittest.mock import MagicMock, patch

# Import the module under test
from aletheia.app.watchers import mesh_subscriber


def test_mesh_subscriber_enqueues_job():
    # Create a fake Redis client and patch it into the module
    mock_redis = MagicMock()
    with patch.object(mesh_subscriber, "redis_client", mock_redis):
        event = {"task_id": "123e4567-e89b-12d3-a456-426614174000", "timestamp": "2026-04-26T19:00:00Z"}
        # Call the async handler; we can run it in the event loop via asyncio.run
        import asyncio
        asyncio.run(mesh_subscriber.handle_mesh_task_complete(event))
        # Verify that rpush was called with the correct queue name and payload
        mock_redis.rpush.assert_called_once()
        args, _ = mock_redis.rpush.call_args
        queue_name, payload = args
        assert queue_name == mesh_subscriber.QUEUE_NAME
        # Payload should be JSON-encoded version of the event
        decoded = json.loads(payload)
        assert decoded == event
