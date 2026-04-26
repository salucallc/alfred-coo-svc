# Quarantine state handling for fleet endpoints
"""
Provides simple in‑memory tracking of an endpoint's `mode_state`.
In the real service this would be integrated with the heartbeat and key‑rotation logic.
Here we expose a tiny API used by the unit test and the mcctl command.
"""

# In‑memory store of endpoint states. Keys are endpoint IDs, values are the mode_state string.
_endpoint_states: dict[str, str] = {}


def set_endpoint_state(endpoint_id: str, mode_state: str) -> None:
    """Set the `mode_state` for the given endpoint.

    Args:
        endpoint_id: Identifier of the endpoint.
        mode_state: One of "normal", "degraded", "quarantine", etc.
    """
    _endpoint_states[endpoint_id] = mode_state


def get_endpoint_state(endpoint_id: str) -> str:
    """Return the current `mode_state` for an endpoint.

    If the endpoint has never been seen, it defaults to ``"normal"``.
    """
    return _endpoint_states.get(endpoint_id, "normal")


def expire_api_key(endpoint_id: str) -> None:
    """Simulate an API‑key expiry that forces the endpoint into quarantine.

    In the production code this would be triggered by the key‑rotation check.
    """
    set_endpoint_state(endpoint_id, "quarantine")


def unquarantine(endpoint_id: str) -> None:
    """Recover an endpoint from quarantine back to normal operation."""
    set_endpoint_state(endpoint_id, "normal")
