import time
from typing import Mapping

TOKEN_EXPIRED_ERROR = '{"error":"token_expired"}'

def validate_iat_claim(claims: Mapping[str, int]) -> None:
    """Validate the 'iat' claim is present and not older than 24h.

    Raises:
        ValueError: with JSON error body if validation fails.
    """
    iat = claims.get('iat')
    if iat is None:
        raise ValueError(TOKEN_EXPIRED_ERROR)
    now = int(time.time())
    if now - iat > 86400:
        raise ValueError(TOKEN_EXPIRED_ERROR)
