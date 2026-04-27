import time
import json
import pytest
from src.alfred_coo.auth.ttl_validator import (
    validate_token_payload,
    TokenExpiredError,
    unauthorized_error_body,
)

def make_payload(iat: int | None) -> dict:
    payload = {}
    if iat is not None:
        payload["iat"] = iat
    return payload

def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = make_payload(now - 3600)  # 1 hour ago
    # Should not raise
    validate_token_payload(payload)

def test_expired_iat_returns_401():
    now = int(time.time())
    payload = make_payload(now - 90000)  # 25 hours ago
    with pytest.raises(TokenExpiredError):
        validate_token_payload(payload)
    assert unauthorized_error_body() == json.dumps({"error": "token_expired"})

def test_missing_iat_returns_401():
    payload = make_payload(None)
    with pytest.raises(TokenExpiredError):
        validate_token_payload(payload)
    assert unauthorized_error_body() == json.dumps({"error": "token_expired"})
