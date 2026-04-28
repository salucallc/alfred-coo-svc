"""TTL validator for scoped OAuth2 tokens.

The validator checks the `iat` (issued‑at) claim of a token and enforces a
maximum lifetime of 24 hours (86400 seconds). If the claim is missing or the
token is older than this limit a ``401 Unauthorized`` response with the exact
JSON payload `{"error":"token_expired"}` is raised.

The implementation uses ``httpx.Response`` so that callers can raise the
standard ``httpx.HTTPStatusError`` which integrates cleanly with existing
error‑handling code.
"""

import time
from typing import Optional

import httpx

_TTL_SECONDS = 86400


def validate_iat(iat: Optional[int]) -> None:
    """Validate the ``iat`` claim.

    Args:
        iat: The ``iat`` timestamp from the token payload, or ``None`` if the
            claim is absent.

    Raises:
        httpx.HTTPStatusError: If the token is expired or the claim is missing.
    """
    if iat is None:
        raise httpx.HTTPStatusError(
            "token expired",
            request=None,
            response=httpx.Response(401, json={"error": "token_expired"}),
        )
    now = int(time.time())
    if now - iat > _TTL_SECONDS:
        raise httpx.HTTPStatusError(
            "token expired",
            request=None,
            response=httpx.Response(401, json={"error": "token_expired"}),
        )
    # Token is within TTL – nothing to do.
