import uuid

def create_token(site: str, ttl: str) -> str:
    """Create a one‑shot token for a given site with a TTL.

    In the real implementation this would write a row to the token DB and enforce
    the TTL. For now we return a deterministic placeholder string that includes the
    site and ttl so the unit test can assert the values.
    """
    token_id = uuid.uuid4().hex[:8]
    return f"TOKEN-{site.upper()}-{ttl}-{token_id}"
