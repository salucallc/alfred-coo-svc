import requests
from typing import List

AUTHELIA_TOKEN_URL = "http://localhost:9091/api/oauth2/token"

def get_scoped_token(scopes: List[str]) -> str:
    """
    Obtain an OAuth2 client_credentials token from Authelia with the requested scopes.
    Returns the access token string.
    """
    payload = {
        "grant_type": "client_credentials",
        "client_id": "ops-14-scoped",
        "client_secret": "<generated-secret>",  # placeholder; injected via env
        "scope": " ".join(scopes),
    }
    resp = requests.post(AUTHELIA_TOKEN_URL, data=payload, timeout=5)
    resp.raise_for_status()
    return resp.json()["access_token"]
