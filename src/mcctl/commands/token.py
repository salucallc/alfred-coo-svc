import sys

def create_token(site: str, ttl: str) -> None:
    """Create a one‑shot token for a given site.
    This is a placeholder implementation that prints a dummy token.
    """
    # In a real implementation we would call the backend API.
    dummy_token = f"dummy-{site}-{ttl}"
    print(dummy_token)
