'''TTL validation utilities for scoped OAuth2 tokens.'''

import time
import json
from http import HTTPStatus

# Token Time‑To‑Live in seconds (24 hours)
TTL_SECONDS = 86400

class TokenExpiredError(Exception):
    """Raised when a token's iat claim is older than the allowed TTL."""
    pass

def _now_unix() -> int:
    """Return the current Unix timestamp as an int."""
    return int(time.time())

def validate_iat(iat: int) -> None:
    """Validate that the issued‑at timestamp is within the allowed TTL.

    Raises:
        TokenExpiredError: if the token is older than TTL_SECONDS.
    """
    if _now_unix() - iat > TTL_SECONDS:
        raise TokenExpiredError

def validate_token_payload(payload: dict) -> None:
    """Validate the token payload's iat claim.

    The function enforces the 24h TTL and denies tokens missing the iat claim.

    Args:
        payload: The decoded JWT payload as a dict.

    Raises:
        TokenExpiredError: if the token is expired or missing iat.
    """
    iat = payload.get('iat')
    if iat is None:
        raise TokenExpiredError
    validate_iat(iat)

def unauthorized_error_body() -> str:
    """Return the JSON error body for an expired or missing‑iat token."""
    return json.dumps({"error": "token_expired"})
