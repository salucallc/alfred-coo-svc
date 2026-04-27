import time
import uuid
from typing import List

def generate_scoped_token(scopes: List[str], ttl_seconds: int = 86400) -> dict:
    """
    Generate a scoped token with given scopes and TTL.
    Returns a dict with token data.
    """
    expiration = int(time.time()) + ttl_seconds
    token = {
        "jti": str(uuid.uuid4()),
        "scopes": scopes,
        "exp": expiration,
    }
    # In real implementation, sign the token with Authelia secret.
    return token

def token_is_valid(token: dict, required_scope: str) -> bool:
    """
    Check if token includes required_scope and is not expired.
    """
    now = int(time.time())
    if now >= token.get("exp", 0):
        return False
    return required_scope in token.get("scopes", [])
