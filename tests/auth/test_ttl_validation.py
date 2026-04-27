import time
import pytest
from ttl_validator import validate_token_iat

def test_valid_recent_iat_passes():
    recent_iat = int(time.time()) - 3600  # 1 hour ago
    assert validate_token_iat(recent_iat) is True

def test_expired_iat_returns_401():
    expired_iat = int(time.time()) - 90000  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        validate_token_iat(expired_iat)
    assert str(exc.value) == '{"error":"token_expired"}'

def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        validate_token_iat(None)
    assert str(exc.value) == '{"error":"token_expired"}'
