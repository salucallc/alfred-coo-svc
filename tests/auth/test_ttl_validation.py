import time
import pytest
from alfred_coo.auth.ttl_validator import validate_token, TokenExpiredError

def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = {"iat": now - 3600}  # 1 hour ago
    # Should not raise
    validate_token(payload)

def test_expired_iat_returns_401():
    now = int(time.time())
    payload = {"iat": now - 90000}  # 25 hours ago
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert exc.value.response == {"error": "token_expired"}

def test_missing_iat_returns_401():
    payload = {}
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert exc.value.response == {"error": "token_expired"}
