import time
from typing import Optional, Tuple

# TTL constant: 24 hours in seconds
TTL_SECONDS = 86400  # 24 * 3600

def validate_iat(iat: Optional[int]) -> Tuple[int, dict]:
    """Validate the `iat` claim of a token.

    Returns a tuple of (status_code, response_body). On failure the body must be exactly
    ``{"error":"token_expired"}`` with a 401 status. On success returns 200 with an empty
    body.
    """
    if iat is None:
        return 401, {"error": "token_expired"}
    now_unix = int(time.time())
    if now_unix - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {}
