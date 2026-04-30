from dataclasses import dataclass
from typing import Any, Dict

# Placeholder for DB interaction
def _persist_external_agent(tenant_id: str, mesh_session_id: str, direction: str, config: Dict[str, Any]) -> int:
    """Insert a row into external_agents table and return the new record ID.
    This is a stub; in production this would interact with the database."""
    return 1

@dataclass
class RegistrationResult:
    agent_id: int
    direction: str
    soulkey: str

def register_plugin(
    tenant_id: str,
    mesh_session_id: str,
    config: Dict[str, Any],
) -> RegistrationResult:
    """
    Register a plugin for an external agent.

    Args:
        tenant_id: Identifier of the tenant.
        mesh_session_id: Mesh session identifier.
        config: Arbitrary configuration dict. Must contain a ``direction`` key
                with one of ``"inbound"``, ``"outbound"`` or ``"bidirectional"``.

    Returns:
        RegistrationResult containing the persisted agent identifier,
        the direction and a plain‑text soulkey.
    """
    direction = config.get("direction")
    if direction not in {"inbound", "outbound", "bidirectional"}:
        raise ValueError("Invalid direction")

    agent_id = _persist_external_agent(tenant_id, mesh_session_id, direction, config)
    soulkey = f"soulkey-{agent_id}"
    return RegistrationResult(agent_id=agent_id, direction=direction, soulkey=soulkey)
