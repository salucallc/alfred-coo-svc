import time
import pytest
from alfred_coo.auth.ttl_validator import validate_token_ttl, TokenExpiredError

def test_valid_recent_iat_passes():
    iat = int(time.time()) - 3600  # 1 hour ago
    # should not raise
    validate_token_ttl(iat)

def test_expired_iat_returns_401():
    iat = int(time.time()) - 90000  # > 24h
    with pytest.raises(TokenExpiredError) as exc:
        validate_token_ttl(iat)
    assert exc.value.status_code == 401
    assert exc.value.body == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc:
        validate_token_ttl(None)
    assert exc.value.status_code == 401
    assert exc.value.body == {"error": "token_expired"}
