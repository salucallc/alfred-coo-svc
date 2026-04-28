import time
from fastapi import HTTPException

def enforce_ttl(iat: int | None) -> None:
    """Enforce 24h TTL on a token's iat claim.
    Raises HTTPException(401) with {"error":"token_expired"} if missing or expired.
    """
    if iat is None:
        raise HTTPException(status_code=401, detail={"error":"token_expired"})
    now = int(time.time())
    if now - iat > 86400:
        raise HTTPException(status_code=401, detail={"error":"token_expired"})
