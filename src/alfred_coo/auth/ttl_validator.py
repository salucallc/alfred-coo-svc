import time

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat claim."""
    def __init__(self):
        self.response = {"error": "token_expired"}
        super().__init__("token_expired")

def _now_unix() -> int:
    return int(time.time())

def validate_iat(iat: int) -> None:
    """Validate that the iat claim is within 24 hours.

    Raises TokenExpiredError if the token is older than 86400 seconds.
    """
    if _now_unix() - iat > 86400:
        raise TokenExpiredError()

def validate_token(payload: dict) -> None:
    """Validate a token payload.

    - Payload must contain an integer ``iat`` claim.
    - ``iat`` must be within 24h of current time.
    - Otherwise raise TokenExpiredError.
    """
    iat = payload.get("iat")
    if not isinstance(iat, int):
        raise TokenExpiredError()
    validate_iat(iat)
