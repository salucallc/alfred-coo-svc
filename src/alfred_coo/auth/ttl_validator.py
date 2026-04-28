import time
from typing import Optional

ERROR_RESPONSE = {"error": "token_expired"}

class TokenExpired(RuntimeError):
    """Raised when a token is expired or missing the ``iat`` claim."""
    pass

def validate_iat(iat: Optional[int]) -> None:
    """Validate the ``iat`` (issued‑at) claim of a token.

    - If ``iat`` is missing or the token is older than 24 hours (86400 s),
      raise ``TokenExpired``.
    - Otherwise the token is considered fresh.
    """
    if iat is None:
        raise TokenExpired
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpired
