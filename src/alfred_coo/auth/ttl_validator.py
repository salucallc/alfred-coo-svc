import base64
import json
import time
from typing import Any

class TokenExpiredError(Exception):
    """Exception raised when a token is expired or missing iat claim."""
    def __init__(self, message: str = "{\"error\":\"token_expired\"}"):
        self.message = message
        super().__init__(self.message)

def _decode_jwt(token: str) -> Any:
    """Decode JWT payload without verification. Returns payload dict or raises ValueError."""
    try:
        parts = token.split('.')
        if len(parts) < 2:
            raise ValueError("Invalid JWT")
        payload_b64 = parts[1]
        padding = '=' * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode('utf-8')
        return json.loads(payload_json)
    except Exception as e:
        raise ValueError(f"Failed to decode JWT: {e}")

def validate_scoped_token(token: str) -> None:
    """Validate token TTL. Raises TokenExpiredError on expiry or missing iat."""
    payload = _decode_jwt(token)
    iat = payload.get('iat')
    if iat is None:
        raise TokenExpiredError()
    now = int(time.time())
    if now - iat > 86400:
        raise TokenExpiredError()
    # otherwise valid – do nothing
