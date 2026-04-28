import time
import json

# TTL in seconds (24 hours)
TTL_SECONDS = 86400

def validate_iat(iat: int | None) -> None:
    """Validate the `iat` claim against the TTL.

    Raises:
        ValueError: If the token is expired or missing the `iat` claim.
    """
    now = int(time.time())
    if iat is None:
        raise ValueError(json.dumps({"error": "token_expired"}))
    if now - iat > TTL_SECONDS:
        raise ValueError(json.dumps({"error": "token_expired"}))
