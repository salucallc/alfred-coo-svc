# SPDX-License-Identifier: MIT
"""Memory push loop for fleet endpoint.

Implements POST /v1/fleet/memory/push with batching and basic error handling.
"""
import json
import time
from typing import List, Dict, Any

import requests

API_URL = "http://localhost:8080/v1/fleet/memory/push"
BATCH_SIZE = 100  # reasonable batch size


def _make_payload(endpoint_id: str, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"endpoint_id": endpoint_id, "batch": batch}


def push_memory(endpoint_id: str, memory_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Push a list of memory items to the hub.

    Args:
        endpoint_id: Identifier of this endpoint.
        memory_items: List of memory dicts with required keys (local_seq, op, memory).

    Returns:
        List of server responses for each batch.
    """
    responses = []
    for i in range(0, len(memory_items), BATCH_SIZE):
        batch = memory_items[i:i + BATCH_SIZE]
        payload = _make_payload(endpoint_id, batch)
        try:
            r = requests.post(API_URL, json=payload, timeout=5)
            r.raise_for_status()
            responses.append(r.json())
        except requests.RequestException as e:
            # Simple retry after short delay
            time.sleep(1)
            try:
                r = requests.post(API_URL, json=payload, timeout=5)
                r.raise_for_status()
                responses.append(r.json())
            except requests.RequestException as e2:
                responses.append({"error": str(e2)})
    return responses
