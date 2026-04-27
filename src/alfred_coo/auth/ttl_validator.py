import time

class TokenExpiredError(Exception):
    """Exception representing a token expiration error with the required response body."""

    def __init__(self):
        # Store the exact JSON body required by the API contract.
        self.body = "{\"error\":\"token_expired\"}"
        super().__init__(self.body)

    def to_response(self):
        """Return a dict suitable for JSON serialization as the HTTP 401 body."""
        return {"error": "token_expired"}


def validate_token(iat: int | None) -> None:
    """Validate the ``iat`` (issued‑at) claim of an OAuth2 token.

    - If ``iat`` is missing or the token is older than 86400 seconds (24 h),
      a :class:`TokenExpiredError` is raised.
    - Otherwise the function returns ``None`` indicating the token is within the
      allowed TTL.
    """
    now = int(time.time())
    if iat is None:
        raise TokenExpiredError()
    if now - iat > 86400:
        raise TokenExpiredError()
    # Token is fresh; no action needed.
    return None
