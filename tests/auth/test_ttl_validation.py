import json
import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

def test_valid_recent_iat_passes():
    now = int(time.time())
    # token issued 1 hour ago should not raise
    validate_iat(now - 3600)

def test_expired_iat_returns_401():
    now = int(time.time())
    with pytest.raises(TokenExpiredError) as excinfo:
        validate_iat(now - 90000)  # 25 hours ago
    exc = excinfo.value
    assert exc.status_code == 401
    assert json.loads(exc.body) == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as excinfo:
        validate_iat(None)
    exc = excinfo.value
    assert exc.status_code == 401
    assert json.loads(exc.body) == {"error": "token_expired"}
