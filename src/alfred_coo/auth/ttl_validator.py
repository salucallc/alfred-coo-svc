import time

TTL_SECONDS = 86400  # 24*3600 seconds

class TokenExpiredError(Exception):
    """Raised when a token is expired or missing iat claim."""
    pass

def validate_token_iat(iat: int) -> None:
    """Validate the `iat` (issued-at) claim against TTL.

    Args:
        iat: Issued-at timestamp (Unix epoch seconds).

    Raises:
        TokenExpiredError: If token is older than TTL_SECONDS.
    """
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        raise TokenExpiredError('token_expired')

def validate_token(payload: dict) -> None:
    """Validate token payload contains a non‑expired iat claim.

    Raises:
        TokenExpiredError: If `iat` is missing or token is expired.
    """
    iat = payload.get('iat')
    if iat is None:
        raise TokenExpiredError('token_expired')
    validate_token_iat(iat)
