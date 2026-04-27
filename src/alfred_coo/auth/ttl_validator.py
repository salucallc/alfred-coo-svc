import time
from typing import Mapping

class TokenValidationError(RuntimeError):
    """Raised when a token fails TTL validation."""

def validate_iat(payload: Mapping[str, int]) -> None:
    """Validate the ``iat`` (issued‑at) claim of an OAuth2 token.

    The token is considered expired when the difference between the current
    Unix time and the ``iat`` claim is greater than 86 400 seconds (24 h).

    * If the ``iat`` claim is missing, the token is rejected.
    * If the token is older than 24 h, the token is rejected.

    In either case a ``TokenValidationError`` is raised with a message that
    matches the APE/V acceptance criteria.
    """
    iat = payload.get("iat")
    if iat is None:
        raise TokenValidationError("missing iat")
    now = int(time.time())
    if now - iat > 86_400:
        raise TokenValidationError("expired")
