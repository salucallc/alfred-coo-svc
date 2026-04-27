# TTL validator for scoped OAuth2 tokens.
#
# Validates that the token's `iat` (issued-at) claim is not older than 24 hours.
#
# Returns (status_code, body) where body is a dict.

import time
from typing import Tuple, Dict, Any

TTL_SECONDS = 86400  # 24 hours

def validate_iat(token: Dict[str, Any]) -> Tuple[int, Dict[str, str]]:
    """Validate the ``iat`` claim of a token.

    Returns a tuple of (status_code, body). On success the status_code is 200
    and the body is an empty dict. On failure the status_code is 401 and the
    body is exactly ``{"error":"token_expired"}``.
    """
    iat = token.get("iat")
    if iat is None:
        return 401, {"error": "token_expired"}
    now_unix = int(time.time())
    if now_unix - int(iat) > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {}
