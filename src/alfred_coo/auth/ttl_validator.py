import time
import json
from typing import Tuple, Dict

# Constant for 24 hours in seconds
TTL_SECONDS = 86400

def validate_iat(token_payload: Dict) -> Tuple[int, str]:
    """Validate the `iat` claim of an OAuth2 token.

    Returns a tuple of (status_code, response_body_json_string).
    On success (token within TTL) returns 200 with empty JSON object.
    On failure (missing or expired) returns 401 with exact error body.
    """
    iat = token_payload.get("iat")
    if iat is None:
        return 401, json.dumps({"error": "token_expired"})
    now = int(time.time())
    if now - iat > TTL_SECONDS:
        return 401, json.dumps({"error": "token_expired"})
    return 200, json.dumps({})
