# SPDX-License-Identifier: MIT
"""Tests for the fleet endpoint memory push implementation."""

import json
from unittest import mock

from alfred_coo.fleet_endpoint.memory_push import push_memory


def generate_memory_items(count):
    items = []
    for seq in range(1, count + 1):
        items.append({
            "local_seq": seq,
            "op": "upsert",
            "memory": {
                "memory_id": f"mem-{seq}",
                "tenant_id": "test",
                "topics": ["test"],
                "content": f"data-{seq}",
                "content_hash": f"hash-{seq}",
                "created_at": "2026-01-01T00:00:00Z",
                "source": {"persona": "endpoint", "agent_id": "agent-1"}
            }
        })
    return items

@mock.patch('alfred_coo.fleet_endpoint.memory_push.requests.post')
def test_push_memory_success(mock_post):
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"accepted_up_to_local_seq": 10}

    endpoint_id = "ep-test"
    items = generate_memory_items(10)
    responses = push_memory(endpoint_id, items)
    assert len(responses) == 1
    assert responses[0]["accepted_up_to_local_seq"] == 10
    # Ensure POST called with correct URL and payload
    args, kwargs = mock_post.call_args
    assert args[0].endswith('/v1/fleet/memory/push')
    payload = kwargs['json']
    assert payload['endpoint_id'] == endpoint_id
    assert len(payload['batch']) == 10

@mock.patch('alfred_coo.fleet_endpoint.memory_push.requests.post')
def test_push_memory_retry_on_failure(mock_post):
    # First call raises, second succeeds
    mock_post.side_effect = [Exception('network'), mock.Mock(status_code=200, json=mock.Mock(return_value={"accepted_up_to_local_seq": 5}))]
    endpoint_id = "ep-retry"
    items = generate_memory_items(5)
    responses = push_memory(endpoint_id, items)
    assert len(responses) == 1
    assert responses[0]["accepted_up_to_local_seq"] == 5
    assert mock_post.call_count == 2
