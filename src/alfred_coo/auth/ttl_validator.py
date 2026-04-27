import time
from typing import Tuple, Dict

# TTL constant: 86400 seconds (24 hours)
TTL_SECONDS = 86_400

def validate_iat(iat: int | None) -> Tuple[int, Dict[str, str]]:
    """Validate the ``iat`` claim of a token.

    Returns a tuple ``(status_code, body)`` where ``body`` is a JSON‑serialisable
    dict.  Missing ``iat`` or an ``iat`` older than 24 hours results in ``401``
    with the exact payload ``{"error":"token_expired"}``.  Otherwise a ``200``
    with an empty body is returned.
    """
    if iat is None:
        return 401, {"error": "token_expired"}
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {}
