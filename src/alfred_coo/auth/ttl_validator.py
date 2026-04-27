import time
from typing import Optional

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat claim."""
    pass

def validate_iat(iat: Optional[int]) -> None:
    """Validate the `iat` (issued-at) claim of a token.

    Args:
        iat: Unix timestamp of token issuance, or None if claim missing.
    Raises:
        TokenExpiredError: If the token is older than 24 hours or the iat claim is absent.
    """
    if iat is None:
        raise TokenExpiredError('{"error":"token_expired"}')
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpiredError('{"error":"token_expired"}')
