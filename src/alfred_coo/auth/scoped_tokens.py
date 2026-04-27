"""
Utility for obtaining and using scoped OAuth2 client credentials tokens.
"""

import os
import requests

TOKEN_ENDPOINT = os.getenv("AUTHELIA_OAUTH_TOKEN_URL", "http://localhost:9091/api/oauth2/token")
CLIENT_ID = os.getenv("OPS_14_CLIENT_ID", "ops-14-client")
CLIENT_SECRET = os.getenv("OPS_14_CLIENT_SECRET", "change-me")

def get_token(scopes: list[str]) -> str:
    """Obtain a client credentials token for the given scopes."""
    data = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": " ".join(scopes),
    }
    resp = requests.post(TOKEN_ENDPOINT, data=data)
    resp.raise_for_status()
    return resp.json()["access_token"]

def api_request(method: str, url: str, token: str, **kwargs):
    """Perform an authenticated request using the provided token."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, url, headers=headers, **kwargs)

def can_read_memory(token: str) -> bool:
    r = api_request("GET", "http://localhost:8000/api/memory/search", token)
    return r.status_code == 200

def can_write_memory(token: str) -> bool:
    r = api_request("POST", "http://localhost:8000/api/memory/write", token, json={"key": "x", "value": "y"})
    return r.status_code == 200
