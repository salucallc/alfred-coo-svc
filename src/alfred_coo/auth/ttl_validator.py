import time
from typing import Optional

# Constant for 24 hours in seconds
TTL_SECONDS = 86400

def validate_iat(iat: Optional[int]) -> None:
    """Validate the `iat` (issued-at) claim of a token.

    Raises a ``ValueError`` with a JSON-serializable payload matching the
    required error response when the token is expired or the claim is missing.
    """
    if iat is None:
        raise ValueError('{"error":"token_expired"}')
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        raise ValueError('{"error":"token_expired"}')
    # otherwise token is fresh – no exception
