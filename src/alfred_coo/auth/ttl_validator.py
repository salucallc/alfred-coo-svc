import time
from typing import Optional, Dict

TTL_SECONDS = 86400  # 24 hours

def validate_iat(iat: Optional[int]) -> Optional[Dict[str, str]]:
    """Validate the `iat` (issued‑at) claim.

    Returns ``None`` if the token is within the allowed TTL.
    Returns a dict with the error payload if the token is missing or expired.
    """
    if iat is None:
        return {"error": "token_expired"}
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        return {"error": "token_expired"}
    return None
