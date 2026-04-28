import time
from typing import Optional

_TTL_SECONDS = 86400  # 24 hours

def check_ttl(iat: Optional[int]) -> None:
    """Validate the token's issued‑at timestamp.

    Args:
        iat: Unix timestamp of token issuance, or ``None`` if missing.

    Raises:
        ValueError: If the token is expired or the ``iat`` claim is missing.
        The exception message is exactly the HTTP 401 JSON body expected by the
        acceptance criteria.
    """
    if iat is None:
        raise ValueError('{"error":"token_expired"}')
    now = int(time.time())
    if now - iat > _TTL_SECONDS:
        raise ValueError('{"error":"token_expired"}')
    # otherwise valid – no action needed
