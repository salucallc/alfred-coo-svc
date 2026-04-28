import time

class TokenExpiredError(Exception):
    """Exception representing a 401 token expired response."""
    def __init__(self):
        super().__init__('{"error":"token_expired"}')

def validate_iat(token_claims: dict) -> None:
    """Validate the `iat` claim of an OAuth2 token.

    Raises TokenExpiredError if the token is missing `iat` or if the token is older than 86400 seconds.
    """
    iat = token_claims.get('iat')
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpiredError()
    # token is within TTL: valid
