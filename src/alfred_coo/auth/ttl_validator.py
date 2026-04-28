import time
import httpx
from httpx import Response, HTTPStatusError

_TTL_SECONDS = 86400  # 24 hours

def _error_response():
    return {"error": "token_expired"}

def validate_iat(iat: int | None) -> None:
    """Validate the `iat` (issued-at) claim of an OAuth2 token.

    Raises:
        HTTPStatusError: If the token is expired or missing the `iat` claim.
    """
    now = int(time.time())
    if iat is None or now - iat > _TTL_SECONDS:
        raise HTTPStatusError(
            "401 Unauthorized",
            request=None,
            response=Response(401, json=_error_response()),
        )
    return None
