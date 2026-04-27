import time
from fastapi import HTTPException

TOKEN_TTL_SECONDS = 86400  # 24 hours

def validate_ttl(iat: int | None) -> None:
    """Validate the token's issued-at timestamp.

    Raises HTTPException(401) with body {"error":"token_expired"} if the token is missing
    the `iat` claim or if it is older than 24 hours.
    """
    if iat is None:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    now = int(time.time())
    if now - iat > TOKEN_TTL_SECONDS:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
