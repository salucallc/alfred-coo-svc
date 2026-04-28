import time

# TTL in seconds for scoped tokens (24 hours)
TTL_SECONDS = 86400

def validate_iat(token_payload: dict) -> None:
    """Validate the `iat` (issued-at) claim of a token.

    Raises:
        ValueError: If `iat` is missing or token age exceeds TTL_SECONDS.
    """
    iat = token_payload.get("iat")
    if iat is None:
        # Missing iat considered expired per spec
        raise ValueError("token_expired")
    now_unix = int(time.time())
    if now_unix - iat > TTL_SECONDS:
        raise ValueError("token_expired")
    # Token is within TTL; no action needed.
    return None
