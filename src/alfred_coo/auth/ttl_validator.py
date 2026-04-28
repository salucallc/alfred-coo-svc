import time
from typing import Tuple, Dict

TTL_SECONDS = 86400  # 24 hours

def validate_token_iat(token: Dict) -> Tuple[int, Dict]:
    """Validate the `iat` claim of an OAuth2 token.

    Returns a tuple of (status_code, body). A status of 200 indicates the token is
    fresh; 401 indicates expiration or missing claim, with a body of
    `{"error":"token_expired"}`.
    """
    iat = token.get("iat")
    if iat is None:
        return 401, {"error": "token_expired"}
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, token
