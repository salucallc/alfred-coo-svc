import time
import httpx

# TTL of 24 hours expressed in seconds
TOKEN_TTL_SECONDS = 86400

def enforce_ttl(iat: int | None) -> None:
    """Validate the token's issued‑at claim.

    Args:
        iat: The ``iat`` claim from a JWT token, expressed as a Unix timestamp.
             If ``None`` the claim is considered missing.

    Raises:
        httpx.HTTPStatusError: If the token is missing the ``iat`` claim or
            the token is older than ``TOKEN_TTL_SECONDS``.
    """
    now = int(time.time())
    if iat is None or now - iat > TOKEN_TTL_SECONDS:
        response = httpx.Response(
            status_code=401,
            json={"error": "token_expired"},
            request=httpx.Request("GET", "http://example.invalid"),
        )
        raise httpx.HTTPStatusError("token expired", request=response.request, response=response)
    # Token is within TTL – no exception raised.
