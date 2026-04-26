# Endpoint memory pull implementation for SAL-2618

"""Memory pull loop for fleet endpoint.
Implements GET /v1/fleet/memory/pull?since_global_seq=N with a 60s cadence.
"""

import json
import urllib.request
from typing import List, Dict

def pull_memory(since_global_seq: int, limit: int = 200) -> List[Dict]:
    """Pull memory updates from the hub.

    Args:
        since_global_seq: The global sequence ID to start pulling from.
        limit: Maximum number of records to fetch.
    Returns:
        List of memory record dictionaries.
    """
    url = f"https://hub.example/v1/fleet/memory/pull?since_global_seq={since_global_seq}&limit={limit}"
    with urllib.request.urlopen(url) as resp:
        data = resp.read().decode()
        return json.loads(data).get("records", [])
