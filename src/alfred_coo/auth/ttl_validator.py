import time
from fastapi import HTTPException
from fastapi.responses import JSONResponse

# TTL constant for scoped tokens (24 hours in seconds)
TTL_SECONDS = 86400

def _error_response() -> JSONResponse:
    """Return the standardized 401 error response for expired or missing iat."""
    return JSONResponse(status_code=401, content={"error": "token_expired"})

def validate_token_iat(token: dict) -> None:
    """Validate the `iat` (issued‑at) claim of a scoped token.

    Args:
        token: Decoded token payload dictionary.

    Raises:
        HTTPException: With status 401 and body {"error":"token_expired"} if the token
        is missing the `iat` claim or if it is older than 24 hours.
    """
    iat = token.get("iat")
    now = int(time.time())
    if iat is None:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    try:
        iat_int = int(iat)
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    if now - iat_int > TTL_SECONDS:
        raise HTTPException(status_code=401, detail={"error": "token_expired"})
    return None
