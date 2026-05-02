from fastapi import APIRouter
from datetime import datetime, timezone, timedelta
from typing import List, Dict

router = APIRouter()


def get_mesh_sessions() -> List[Dict]:
    """Fetch mesh session records.

    In the production code this would query the database or another service.
    Here we return an empty list as a placeholder.
    """
    return []


@router.get("/v1/cockpit/state")
def cockpit_state() -> Dict:
    """Return cockpit state including a filtered list of sub‑agents.

    * Only sessions created within the last five minutes are included.
    * Only sessions whose ``name`` starts with ``agent-`` are considered.
    * The result is limited to a maximum of 50 entries.
    * All required fields are returned verbatim.
    The implementation performs a single read via ``get_mesh_sessions`` to avoid N+1 queries.
    """
    now = datetime.now(timezone.utc)
    sessions = get_mesh_sessions()
    subagents = []
    for sess in sessions:
        created_at = sess.get("created_at")
        if not isinstance(created_at, datetime):
            continue
        # Exclude old sessions
        if now - created_at > timedelta(minutes=5):
            continue
        # Exclude non‑agent sessions
        name = sess.get("name", "")
        if not name.startswith("agent-"):
            continue
        # Append required fields (adjust according to actual schema)
        subagents.append({
            "name": name,
            "status": sess.get("status"),
            "created_at": created_at.isoformat(),
        })
        if len(subagents) >= 50:
            break
    return {"subagents": subagents}
