import time
from typing import Tuple, Dict

# TTL constant in seconds (24 hours)
TTL_SECONDS = 86400

def validate_iat(iat: int | None) -> Tuple[int, Dict[str, str]]:
    """Validate the `iat` (issued-at) claim.

    Returns a tuple of (status_code, body_dict). If the token is valid, returns (200, {}).
    If invalid (expired or missing), returns (401, {"error": "token_expired"}).
    """
    now_unix = int(time.time())
    if iat is None:
        return 401, {"error": "token_expired"}
    if now_unix - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {}
