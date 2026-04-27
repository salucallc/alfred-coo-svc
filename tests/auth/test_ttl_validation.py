import time
from unittest import mock

from alfred_coo.auth.ttl_validator import validate_iat, TTL_SECONDS

def test_valid_recent_iat_passes():
    recent_iat = int(time.time()) - (TTL_SECONDS // 2)  # 12 hours ago
    status, body = validate_iat(recent_iat)
    assert status == 200
    assert body == {}

def test_expired_iat_returns_401():
    with mock.patch('time.time', return_value=1000000):
        expired_iat = 1000000 - (TTL_SECONDS + 1)
        status, body = validate_iat(expired_iat)
        assert status == 401
        assert body == {"error": "token_expired"}

def test_missing_iat_returns_401():
    status, body = validate_iat(None)
    assert status == 401
    assert body == {"error": "token_expired"}
