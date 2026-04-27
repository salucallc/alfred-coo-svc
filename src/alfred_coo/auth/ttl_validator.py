import time
from flask import jsonify, make_response
from typing import Optional


def validate_iat(iat: Optional[int]) -> Optional[object]:
    """Validate the `iat` claim of an OAuth2 token.

    Returns ``None`` if the token is within the allowed TTL.
    Returns a Flask ``Response`` with HTTP 401 and the required error body
    if the token is expired or the claim is missing.
    """
    now = int(time.time())
    if iat is None:
        return make_response(jsonify({"error": "token_expired"}), 401)
    if now - iat > 86400:
        return make_response(jsonify({"error": "token_expired"}), 401)
    return None
