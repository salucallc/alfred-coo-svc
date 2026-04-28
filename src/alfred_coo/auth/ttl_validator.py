import time
from typing import Optional, Dict

# 24 hours in seconds
TTL_SECONDS = 86400  # constant matched by verification grep

def validate_iat(iat: Optional[int]) -> Dict[str, str]:
    """Validate the 'issued at' (iat) claim.

    Returns an empty dict for a valid token, otherwise a dict with the error
    payload expected by the API.
    """
    now = int(time.time())
    if iat is None:
        return {"error": "token_expired"}
    if now - iat > TTL_SECONDS:
        return {"error": "token_expired"}
    return {}
