import time
from typing import Optional, Dict

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat."""
    status_code = 401
    body = {"error": "token_expired"}

def validate_iat(iat: Optional[int]) -> None:
    """Validate the iat claim of a JWT.

    Raises:
        TokenExpiredError: if iat is missing or token is older than 24h.
    """
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpiredError()
