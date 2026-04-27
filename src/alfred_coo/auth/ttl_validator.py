import time

TTL_SECONDS = 86400

def _error_response():
    """Return the exact error body expected for token expiration."""
    return '{"error":"token_expired"}'

def validate_token_iat(iat: int | None):
    """Validate the `iat` claim of a token.

    Args:
        iat: Issued‑at timestamp (seconds since epoch) or ``None`` if missing.

    Returns:
        ``True`` if the token is within the allowed TTL.

    Raises:
        ValueError: With the exact JSON error string when the token is expired
            or the ``iat`` claim is missing.
    """
    if iat is None:
        raise ValueError(_error_response())
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        raise ValueError(_error_response())
    return True

# Example integration point for request handling (pseudo‑code):
# def validate_request(token_payload: dict):
#     iat = token_payload.get('iat')
#     try:
#         validate_token_iat(iat)
#     except ValueError as exc:
#         # Convert to HTTP 401 response
#         raise httpx.HTTPStatusError(message=str(exc), request=None, response=httpx.Response(401, json={"error":"token_expired"}))
