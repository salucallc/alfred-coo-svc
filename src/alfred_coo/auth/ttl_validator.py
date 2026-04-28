import time
from fastapi import HTTPException

# Constant for 24‑hour TTL in seconds
TTL_SECONDS = 86400

def enforce_ttl(iat: int | None) -> None:
    """Validate the token's `iat` (issued‑at) claim.

    Raises HTTPException(401) with body {"error": "token_expired"}
    if the claim is missing or older than 24 hours.
    """
    if iat is None:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
