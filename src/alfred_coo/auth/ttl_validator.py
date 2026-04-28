import time

TOKEN_EXPIRED_ERROR = {"error": "token_expired"}

def check_iat(iat: int | None):
    """Return None if token iat is within 24h, else return error dict.

    Args:
        iat: Issued-at timestamp (Unix epoch seconds) or None.
    Returns:
        None if valid, otherwise a dict matching the required 401 body.
    """
    if iat is None:
        return TOKEN_EXPIRED_ERROR
    now = int(time.time())
    if now - iat > 86400:
        return TOKEN_EXPIRED_ERROR
    return None
