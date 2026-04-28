import time
import json
import httpx
from typing import Optional

# TTL constant for 24 hours (in seconds)
TTL_SECONDS = 86400

def enforce_ttl(iat: Optional[int]) -> None:
    """Enforce that a token's issued-at (iat) claim is within the allowed TTL.

    Args:
        iat: The issued-at Unix timestamp of the token, or ``None`` if missing.

    Raises:
        httpx.HTTPStatusError: If the token is expired or missing the iat claim.
    """
    now_unix = int(time.time())
    # Reject if iat is missing or token is too old
    if iat is None or (now_unix - iat) > TTL_SECONDS:
        # Build a minimal HTTP 401 response with the required JSON body
        resp = httpx.Response(
            status_code=401,
            content=json.dumps({"error": "token_expired"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        raise httpx.HTTPStatusError(message="Token expired", request=None, response=resp)
    # Otherwise, token is within TTL – no action needed
    return
