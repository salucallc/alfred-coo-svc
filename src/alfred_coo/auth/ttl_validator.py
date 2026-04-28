import time
from typing import Optional, Tuple, Dict

def validate_iat(iat: Optional[int]) -> Tuple[int, Dict[str, str]]:
    """Validate the `iat` claim against a 24‑hour TTL.

    Returns a tuple of ``(status_code, body)`` where ``body`` is the JSON
    payload to send back to the client.  A missing or expired ``iat`` results
    in a 401 response with exactly ``{"error":"token_expired"}``.
    """
    now = int(time.time())
    if iat is None:
        return 401, {"error": "token_expired"}
    if now - iat > 86400:
        return 401, {"error": "token_expired"}
    return 200, {}
