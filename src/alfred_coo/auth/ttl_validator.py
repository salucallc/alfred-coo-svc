import time

TTL_SECONDS = 86400

class TokenExpiredError(Exception):
    """Exception indicating token has expired or missing iat claim.
    Includes HTTP status code and required JSON error body.
    """
    status_code = 401
    body = '{"error":"token_expired"}'

def validate_token(payload: dict) -> bool:
    """Validate token payload for TTL.

    Args:
        payload: Dictionary representing token claims. Must contain 'iat' (issued-at) timestamp.

    Returns:
        True if token is valid (not expired).

    Raises:
        TokenExpiredError: If 'iat' is missing or token is older than TTL_SECONDS.
    """
    iat = payload.get("iat")
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        raise TokenExpiredError()
    return True
