import time
from typing import Optional, Dict

# Constant for TTL validation (24 hours in seconds)
TTL_SECONDS = 86400

TOKEN_EXPIRED_BODY = {"error": "token_expired"}


def validate_iat(iat: Optional[int]) -> Dict[str, str]:
    """Validate the `iat` (issued‑at) claim of a token.

    Returns an empty dict on success, otherwise returns the error body that
    should be sent with an HTTP 401 response.
    """
    now = int(time.time())
    if iat is None:
        return TOKEN_EXPIRED_BODY
    if now - int(iat) > TTL_SECONDS:
        return TOKEN_EXPIRED_BODY
    return {}
