import time
from typing import Optional, Dict

TTL_SECONDS = 86400  # 24h in seconds

def validate_iat(iat: Optional[int]) -> Dict[str, str]:
    """Validate the `iat` (issued‑at) claim of a token.

    Returns an empty dict for a valid token. Returns a JSON‑serialisable
    error dict matching the required 401 body when the token is expired or
    the claim is missing.
    """
    now = int(time.time())
    if iat is None:
        return {"error": "token_expired"}
    if now - iat > TTL_SECONDS:
        return {"error": "token_expired"}
    return {}
