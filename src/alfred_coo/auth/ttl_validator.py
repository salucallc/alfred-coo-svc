import time
import json
from typing import Optional

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat claim."""

    def __init__(self):
        # Standardized error body per APE/V
        self.body = json.dumps({"error": "token_expired"})
        super().__init__(self.body)


def validate_iat(iat: Optional[int]) -> None:
    """Validate the `iat` (issued‑at) claim of a JWT.

    Args:
        iat: The issued‑at timestamp in Unix seconds, or ``None`` if missing.

    Raises:
        TokenExpiredError: If ``iat`` is missing or the token is older than 24 hours.
    """
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    # 86400 seconds = 24 hours
    if now - iat > 86400:
        raise TokenExpiredError()
    # otherwise token is within the allowed TTL; no exception raised
