import time

def _error_body():
    return {"error": "token_expired"}

def check_iat(iat: int | None) -> None:
    """Validate the iat (issued-at) claim.

    Raises:
        ValueError: with a JSON‑serialisable body indicating the token is expired or missing.
    """
    now = int(time.time())
    if iat is None:
        raise ValueError(str(_error_body()))
    if now - iat > 86400:
        raise ValueError(str(_error_body()))
    # otherwise valid – no return value
