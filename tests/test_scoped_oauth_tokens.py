import pytest
import httpx
from alfred_coo.auth.scoped_tokens import get_token

def test_get_token_success(monkeypatch):
    monkeypatch.setenv("AUTHELIA_CLIENT_ID", "test-client")
    monkeypatch.setenv("AUTHELIA_CLIENT_SECRET", "secret")
    # Mock httpx.post to return a fake token
    def mock_post(url, data=None, headers=None, timeout=None):
        class Resp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"access_token": "mocked-token"}
        return Resp()
    monkeypatch.setattr(httpx, "post", mock_post)
    token = get_token(["soul:memory:read"])
    assert token == "mocked-token"
