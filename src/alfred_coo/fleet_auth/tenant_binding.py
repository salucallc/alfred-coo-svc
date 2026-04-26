def bind_tenant(token_payload: dict, tenant_id: str) -> dict:
    """Attach tenant information to token payload.

    Args:
        token_payload: Original token payload dictionary.
        tenant_id: Tenant identifier to bind.
    Returns:
        Updated payload with tenant information.
    """
    token_payload['tenant'] = {'tenant_id': tenant_id}
    return token_payload
