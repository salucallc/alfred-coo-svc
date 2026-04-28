import time
import json
from typing import Optional

# TTL constant: 24 hours in seconds
TTL_SECONDS = 86400

def validate_iat(iat: Optional[int]) -> None:
    """Validate the `iat` claim of a token.

    Raises:
        ValueError: with JSON body "{"error":"token_expired"}" if the token is expired or iat is missing.
    """
    now_unix = int(time.time())
    if iat is None:
        raise ValueError(json.dumps({"error": "token_expired"}))
    if now_unix - iat > TTL_SECONDS:
        raise ValueError(json.dumps({"error": "token_expired"}))
    # Token is within TTL; no action needed.
