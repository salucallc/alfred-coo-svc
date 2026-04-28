# TTL validator for scoped OAuth2 tokens
import time
import json
from fastapi import HTTPException, status

TOKEN_EXPIRED_BODY = {"error": "token_expired"}

def enforce_ttl(iat: int | None) -> None:
    """Enforce a 24‑hour TTL on the token's ``iat`` claim.

    Raises:
        HTTPException: 401 with body ``{"error":"token_expired"}`` if the token is
        older than 86400 seconds or if ``iat`` is missing.
    """
    if iat is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=json.dumps(TOKEN_EXPIRED_BODY),
            headers={"Content-Type": "application/json"},
        )
    now = int(time.time())
    if now - iat > 86400:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=json.dumps(TOKEN_EXPIRED_BODY),
            headers={"Content-Type": "application/json"},
        )

# Helper for callers that have a decoded token payload dict
def validate_token_iat(payload: dict) -> None:
    """Validate the ``iat`` claim of a token payload.

    Args:
        payload: The token payload after JWT decoding.
    """
    enforce_ttl(payload.get("iat"))
