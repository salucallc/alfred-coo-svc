import time
from http import HTTPStatus

def validate_iat(payload: dict):
    """
    Validate the 'iat' (issued‑at) claim in a token payload.

    Returns a tuple (status_code, body_dict). On success returns (200, {}).
    On failure returns (401, {"error": "token_expired"}).
    """
    iat = payload.get("iat")
    if iat is None:
        return HTTPStatus.UNAUTHORIZED, {"error": "token_expired"}
    if time.time() - iat > 86400:
        return HTTPStatus.UNAUTHORIZED, {"error": "token_expired"}
    return HTTPStatus.OK, {}
