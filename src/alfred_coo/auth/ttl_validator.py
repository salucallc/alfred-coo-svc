import time

class TokenExpiredException(Exception):
    """Exception raised when a token is expired or missing the iat claim."""

    def __init__(self):
        super().__init__("Token expired")
        self.status_code = 401
        self.body = {"error": "token_expired"}


def enforce_iat(iat: int | None) -> None:
    """Validate the `iat` (issued‑at) claim.

    Args:
        iat: Unix timestamp of when the token was issued, or ``None`` if absent.
    Raises:
        TokenExpiredException: If the token is older than 86400 seconds or ``iat`` is missing.
    """
    now_unix = int(time.time())
    # 86400 seconds = 24 hours
    if iat is None or now_unix - iat > 86400:
        raise TokenExpiredException()
    # Token is within the allowed TTL; no action needed.
