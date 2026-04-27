import time
from typing import Dict, Any

# TTL constant for 24 hours in seconds
TTL_SECONDS = 86400

class TokenExpiredError(Exception):
    """Exception representing a 401 token expired response."""
    def __init__(self, message: str = '{"error":"token_expired"}'):
        self.status_code = 401
        self.body = message
        super().__init__(message)

def validate_token(token: Dict[str, Any]) -> None:
    """Validate the token's issuance time (iat).

    Args:
        token: A dict representing the token payload.

    Raises:
        TokenExpiredError: If the token is missing `iat` or is older than TTL.
    """
    iat = token.get('iat')
    now_unix = int(time.time())
    if iat is None:
        raise TokenExpiredError()
    try:
        iat_int = int(iat)
    except (TypeError, ValueError):
        raise TokenExpiredError()
    if now_unix - iat_int > TTL_SECONDS:
        raise TokenExpiredError()
    # Token within TTL; considered valid.
