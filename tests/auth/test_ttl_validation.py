import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

def test_valid_recent_iat_passes():
    recent_iat = int(time.time()) - 3600  # 1 hour ago
    valid, resp = validate_iat(recent_iat)
    assert valid
    assert resp == {}

def test_expired_iat_returns_401():
    expired_iat = int(time.time()) - 25 * 3600  # 25 hours ago
    valid, resp = validate_iat(expired_iat)
    assert not valid
    assert resp == {"error": "token_expired"}

def test_missing_iat_returns_401():
    valid, resp = validate_iat(None)
    assert not valid
    assert resp == {"error": "token_expired"}
