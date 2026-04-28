import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

def test_valid_recent_iat_passes():
    payload = {"iat": time.time() - 3600}
    status, body = validate_iat(payload)
    assert status == 200

def test_expired_iat_returns_401():
    payload = {"iat": time.time() - 25 * 3600}
    status, body = validate_iat(payload)
    assert status == 401
    assert body == {"error": "token_expired"}

def test_missing_iat_returns_401():
    payload = {}
    status, body = validate_iat(payload)
    assert status == 401
    assert body == {"error": "token_expired"}
