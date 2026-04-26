"""Utilities for binding a tenant to fleet authentication flows.

This module provides a minimal placeholder implementation required for the
C‑27 ticket. The real implementation will integrate with the backend
services to persist tenant bindings.
"""

def bind_tenant(tenant_id: str, endpoint_id: str) -> dict:
    """Return a payload representing a tenant‑endpoint binding.

    Args:
        tenant_id: Identifier of the tenant.
        endpoint_id: Identifier of the endpoint.

    Returns:
        A dictionary that would be sent to the backend in a real system.
    """
    return {
        "tenant_id": tenant_id,
        "endpoint_id": endpoint_id,
    }
