import time
from typing import Optional

TTL_SECONDS = 86400
ERROR_BODY = {"error": "token_expired"}

class TokenExpiredError(Exception):
    """Exception indicating token is expired or missing iat claim."""
    def __init__(self):
        super().__init__("token_expired")

def validate_iat(iat: Optional[int]) -> None:
    """Validate the `iat` (issued‑at) claim.

    * If ``iat`` is ``None`` or the token is older than ``TTL_SECONDS``
      seconds, a :class:`TokenExpiredError` is raised.
    * Otherwise the function returns silently.
    """
    now = int(time.time())
    if iat is None:
        raise TokenExpiredError()
    if now - iat > TTL_SECONDS:
        raise TokenExpiredError()
