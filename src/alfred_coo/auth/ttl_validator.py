import time

TTL_SECONDS = 86400  # 24 hours

def validate_iat(iat: int | None) -> tuple[bool, dict]:
    """Validate token issued-at (iat) claim.

    Returns (is_valid, error_body). If invalid, error_body is {"error":"token_expired"}.
    """
    if iat is None:
        return False, {"error": "token_expired"}
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        return False, {"error": "token_expired"}
    return True, {}
