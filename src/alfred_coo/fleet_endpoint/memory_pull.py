# SPDX-License-Identifier: MIT
"""Fleet endpoint memory pull loop."""

import os
import requests

def pull_memory(since_global_seq: int = 0, limit: int = 200) -> dict:
    """Pull memory entries from the hub.

    Args:
        since_global_seq: Global sequence cursor.
        limit: Maximum number of records to fetch.

    Returns:
        The JSON response from the hub.
    """
    hub_url = os.getenv("FLEET_HUB_URL", "https://hub.example")
    api_key = os.getenv("API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"since_global_seq": since_global_seq, "limit": limit}
    resp = requests.get(
        f"{hub_url}/v1/fleet/memory/pull",
        headers=headers,
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
