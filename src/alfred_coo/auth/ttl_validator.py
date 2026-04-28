import time

def validate_iat(iat: int | None):
    """Validate token issued-at timestamp.

    Returns (status_code, body). If the token is missing ``iat`` or is older than 24h (86400 seconds),
    returns HTTP 401 with error payload ``{"error":"token_expired"}``. Otherwise returns 200 with empty body.
    """
    now = int(time.time())
    if iat is None:
        return 401, {"error": "token_expired"}
    if now - iat > 86400:
        return 401, {"error": "token_expired"}
    return 200, {}
