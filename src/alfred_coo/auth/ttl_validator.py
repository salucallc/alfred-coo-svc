import time
from typing import Optional

class TokenExpiredError(Exception):
    """Exception representing a token expiration error with HTTP 401 semantics."""
    def __init__(self):
        self.status_code = 401
        self.body = {"error": "token_expired"}
        super().__init__("token_expired")

def validate_iat(iat: Optional[int]) -> None:
    """Validate the issued-at (iat) claim.

    Raises TokenExpiredError if iat is missing or the token is older than 86400 seconds.
    """
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpiredError()
