import time
import json

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat."""
    status_code = 401
    body = json.dumps({"error": "token_expired"})

def validate_iat(iat: int | None) -> None:
    """Validate a token's issuance time.

    Args:
        iat: Issued‑At claim as Unix epoch seconds, or ``None`` if missing.

    Raises:
        TokenExpiredError: If ``iat`` is missing or the token is older than 24 hours.
    """
    now = int(time.time())
    if iat is None or now - iat > 86400:
        raise TokenExpiredError()
