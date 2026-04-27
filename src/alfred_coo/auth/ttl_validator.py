import time
from typing import Tuple, Optional

# 24‑hour TTL in seconds
TTL_SECONDS = 86400

def _now_unix() -> int:
    """Return current Unix timestamp as an integer.
    A separate function eases testing / freezegun stubbing.
    """
    return int(time.time())

def is_iat_valid(iat: int) -> bool:
    """Check that ``iat`` is not older than ``TTL_SECONDS``.

    Returns ``True`` if the claim is present and within the allowed window.
    """
    if iat is None:
        return False
    return (_now_unix() - iat) <= TTL_SECONDS

def validate_iat(iat: Optional[int]) -> Tuple[bool, dict]:
    """Validate ``iat`` claim according to OPS‑14d rules.

    Returns a tuple ``(is_valid, error_body)`` where ``error_body`` is the JSON
    payload that should be returned with a *401 Unauthorized* response when the
    claim is missing or expired.
    """
    if iat is None or not is_iat_valid(iat):
        return False, {"error": "token_expired"}
    return True, {}
