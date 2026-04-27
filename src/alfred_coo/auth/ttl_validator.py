import time
import httpx

def enforce_ttl(iat: int | None):
    """Enforce a maximum token age of 24 hours.

    Args:
        iat: Issued‑at timestamp (seconds since epoch) or ``None``.

    Raises:
        httpx.HTTPStatusError: with a 401 response and body ``{"error":"token_expired"}``.
    """
    if iat is None:
        raise httpx.HTTPStatusError(
            "token_expired",
            request=None,
            response=httpx.Response(401, json={"error": "token_expired"}),
        )
    now = int(time.time())
    if now - iat > 86400:
        raise httpx.HTTPStatusError(
            "token_expired",
            request=None,
            response=httpx.Response(401, json={"error": "token_expired"}),
        )
