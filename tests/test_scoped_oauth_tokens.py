import pytest
from unittest.mock import patch
from src.alfred_coo.auth.scoped_tokens import get_scoped_token

def test_get_scoped_token_success(requests_mock):
    token_json = {"access_token": "mock-token", "expires_in": 86400}
    requests_mock.post("http://localhost:9091/api/oauth2/token", json=token_json, status_code=200)

    token = get_scoped_token(["soul:memory:read"])
    assert token == "mock-token"
