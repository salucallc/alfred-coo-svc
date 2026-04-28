import time
import json

class TokenExpiredError(Exception):
    """Exception representing a token expiration response.

    The string representation matches the exact JSON body required by the API.
    """

    def __str__(self) -> str:
        return json.dumps({"error": "token_expired"})


def validate_iat(iat: int | None) -> None:
    """Validate the `iat` (issued‑at) claim of a token.

    Args:
        iat: The issued‑at timestamp as an integer Unix epoch, or ``None`` if missing.

    Raises:
        TokenExpiredError: If the token is missing the claim or is older than 24 hours.
    """
    now = int(time.time())
    # Missing claim – deny by default
    if iat is None:
        raise TokenExpiredError()
    # Expired if delta exceeds 86400 seconds (24 h)
    if now - iat > 86400:
        raise TokenExpiredError()
    # otherwise token is within TTL – pass silently
    return
