import time
import pytest
from src.alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

def test_valid_recent_iat_passes():
    now = int(time.time())
    token = {'iat': now - 3600}  # 1 hour ago
    # Should not raise
    validate_iat(token)

def test_expired_iat_returns_401():
    now = int(time.time())
    token = {'iat': now - 90000}  # 25 hours ago
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(token)
    assert str(exc.value) == '{"error":"token_expired"}'

def test_missing_iat_returns_401():
    token = {}
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(token)
    assert str(exc.value) == '{"error":"token_expired"}'
