import json
import time
from httpx import Response

_TTL_SECONDS = 86400  # 24 hours
_ERROR_BODY = json.dumps({"error": "token_expired"})
_ERROR_HEADERS = {"Content-Type": "application/json"}

def check_ttl(iat: int | None) -> Response:
    """Validate the `iat` (issued‑at) claim.

    Returns a 401 Response with a specific error body when the token is expired
    or the claim is missing; otherwise returns a 200 Response.
    """
    if iat is None:
        return Response(401, content=_ERROR_BODY, headers=_ERROR_HEADERS)
    now = int(time.time())
    if now - iat > _TTL_SECONDS:
        return Response(401, content=_ERROR_BODY, headers=_ERROR_HEADERS)
    return Response(200)
