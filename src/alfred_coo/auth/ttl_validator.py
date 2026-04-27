import time
from typing import Dict, Tuple

TTL_SECONDS = 86400  # 24 hours

def validate_token(payload: Dict) -> Tuple[int, Dict]:
    """Validate the token's 'iat' claim.

    Returns a tuple of (status_code, response_body_dict).
    """
    iat = payload.get("iat")
    now = int(time.time())
    if iat is None or now - iat > TTL_SECONDS:
        return 401, {"error": "token_expired"}
    return 200, {"status": "valid"}
