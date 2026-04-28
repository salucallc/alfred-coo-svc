import time
import json

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat."""
    def __init__(self):
        super().__init__(json.dumps({"error": "token_expired"}))

def enforce_ttl(iat: int | None) -> None:
    """
    Enforce a 24‑hour TTL on a token.

    Args:
        iat: Issued‑At claim as a Unix timestamp, or None if missing.

    Raises:
        TokenExpiredError: If the token is missing iat or older than 86400 seconds.
    """
    now = int(time.time())
    if iat is None:
        raise TokenExpiredError()
    if now - iat > 86400:
        raise TokenExpiredError()
