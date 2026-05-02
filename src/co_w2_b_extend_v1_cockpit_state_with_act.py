"""Extension for /v1/cockpit/state to include subagent activity data.

This module provides the _fetch_subagent_activity() function that extends
the cockpit state endpoint with filtered subagent session data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)


def _parse_datetime_iso(value: str | None) -> datetime | None:
    """Parse ISO format datetime string to datetime object."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


async def _fetch_subagent_activity(
    base_url: str, api_key: str, timeout: float = 3.0
) -> List[Dict[str, Any]]:
    """Fetch and filter subagent activity from mesh sessions.

    Returns a list of subagent entries with the following fields:
    - session_id: The session identifier (truncated to 12 chars)
    - current_task: Current task description (from orchestrator if available)
    - age: Age in seconds since last heartbeat
    - last_heartbeat: ISO timestamp of last heartbeat
    - node_id: Node identifier
    - harness: Harness type

    Filters:
    - Excludes sessions older than 5 minutes (300 seconds)
    - Only includes sessions with session_id starting with 'agent-'
    - Caps results at 50 entries (first 50 after filtering)
    - Single mesh_sessions read, no N+1 queries

    Args:
        base_url: Base URL for soul-svc API
        api_key: API key for authentication
        timeout: Request timeout in seconds

    Returns:
        List of filtered subagent activity entries
    """
    url = f"{base_url.rstrip('/')}/v1/mesh/sessions"
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(
            "subagent activity fetch failed: %s: %s",
            type(e).__name__,
            str(e) or "<no message>",
        )
        return []
    
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    now = datetime.now(timezone.utc)
    
    subagents: List[Dict[str, Any]] = []
    
    for session in sessions:
        if not isinstance(session, dict):
            continue
            
        # Filter 1: Must be active
        if session.get("status") != "active":
            continue
            
        session_id = session.get("session_id", "")
        
        # Filter 2: Must start with 'agent-'
        if not session_id.startswith("agent-"):
            continue
            
        # Parse last heartbeat
        last_heartbeat_str = session.get("last_heartbeat")
        last_heartbeat = _parse_datetime_iso(last_heartbeat_str)
        
        if not last_heartbeat:
            continue
            
        # Calculate age in seconds
        age_secs = int((now - last_heartbeat).total_seconds())
        
        # Filter 3: Exclude sessions older than 5 minutes (300 seconds)
        if age_secs > 300:
            continue
            
        # Build subagent entry
        subagent = {
            "session_id": session_id[:12] if len(session_id) > 12 else session_id,
            "current_task": session.get("current_task", "idle"),
            "age": age_secs,
            "last_heartbeat": last_heartbeat_str or "",
            "node_id": session.get("node_id", ""),
            "harness": session.get("harness", ""),
        }
        
        subagents.append(subagent)
        
        # Cap at 50 entries
        if len(subagents) >= 50:
            break
    
    return subagents
