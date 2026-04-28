import time
from typing import Optional

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat."""
    status_code = 401
    body = {"error": "token_expired"}

def validate_token_ttl(iat: Optional[int]) -> None:
    """
    Validate the token's issued-at (iat) claim against a 24h TTL.

    Args:
        iat: The ``iat`` claim as a UNIX timestamp, or ``None`` if missing.

    Raises:
        TokenExpiredError: If the token is missing ``iat`` or the TTL exceeds 86400 seconds.
    """
    now = int(time.time())
    if iat is None or now - iat > 86400:
        raise TokenExpiredError()
    # Token is within TTL; no action needed.
