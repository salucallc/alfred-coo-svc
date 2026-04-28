import time
from typing import Optional, Tuple, Dict

# Time-to-live constant for scoped tokens (24 hours)
TTL_SECONDS = 86400

def validate_iat(iat: Optional[int]) -> Tuple[int, Dict[str, str]]:
    """Validate the `iat` (issued-at) claim of a token.

    Returns a tuple of (status_code, json_body). If the token is missing the
    `iat` claim or the token is older than ``TTL_SECONDS`` the function returns
    a 401 response with the body ``{"error":"token_expired"}``.
    Otherwise a 200 with an empty body is returned.
    """
    now = int(time.time())
    if iat is None:
        return 401, {"error": "token_expired"}
    if now - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {}
