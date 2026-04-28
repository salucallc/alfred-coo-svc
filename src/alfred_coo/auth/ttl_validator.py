import time
from fastapi import HTTPException

# 24 hours in seconds
TTL_SECONDS = 86400  # 24h TTL constant

def validate_iat(iat: int | None) -> None:
    """Validate the `iat` (issued-at) claim.

    Args:
        iat: Unix timestamp of when the token was issued, or ``None``.

    Raises:
        HTTPException: with status 401 and body {"error":"token_expired"} if the
        token is missing the ``iat`` claim or is older than ``TTL_SECONDS``.
    """
    now = int(time.time())
    if iat is None or now - iat > TTL_SECONDS:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    # otherwise considered valid – no return value
