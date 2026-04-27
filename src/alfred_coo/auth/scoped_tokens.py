import os
import base64
from typing import List
import httpx

AUTHELIA_TOKEN_URL = os.getenv("AUTHELIA_TOKEN_URL", "http://localhost:9091/api/oauth2/token")

def get_token(scopes: List[str]) -> str:
    """
    Obtain an OAuth2 access token using client_credentials flow for the given scopes.
    """
    client_id = os.getenv("AUTHELIA_CLIENT_ID", "ops-14-scoped-token")
    client_secret = os.getenv("AUTHELIA_CLIENT_SECRET", "")
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "client_credentials", "scope": " ".join(scopes)}
    resp = httpx.post(AUTHELIA_TOKEN_URL, data=data, headers=headers, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["access_token"]
