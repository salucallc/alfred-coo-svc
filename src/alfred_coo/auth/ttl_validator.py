import time
from typing import Optional

def validate_iat(iat: Optional[int]) -> Optional[dict]:
    """Validate the `iat` (issued‑at) claim.

    Returns ``None`` if the token is within the allowed TTL (24 h).
    Returns a ``{"error": "token_expired"}`` dict otherwise.
    """
    now = int(time.time())
    if iat is None:
        return {"error": "token_expired"}
    if now - iat > 86400:
        return {"error": "token_expired"}
    return None
