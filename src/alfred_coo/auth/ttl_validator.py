import time
from typing import Optional

TTL_SECONDS = 86400  # 24 hours

class TokenExpiredError(RuntimeError):
    """Raised when a token's iat claim is older than allowed TTL."""

    pass

def validate_iat(iat: Optional[int]) -> None:
    """Validate the ``iat`` (issued-at) claim.

    Args:
        iat: Unix timestamp when the token was issued, or ``None`` if missing.

    Raises:
        TokenExpiredError: If ``iat`` is missing or the token is older than 24h.
    """
    now = int(time.time())
    if iat is None:
        raise TokenExpiredError("token_expired")
    if now - iat > TTL_SECONDS:
        raise TokenExpiredError("token_expired")
