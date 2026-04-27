import time
import pytest

from src.alfred_coo.auth.ttl_validator import enforce_iat, TokenExpiredException


def test_valid_recent_iat_passes():
    recent_iat = int(time.time()) - 3600  # 1 hour ago
    # Should not raise any exception
    enforce_iat(recent_iat)


def test_expired_iat_returns_401():
    expired_iat = int(time.time()) - (25 * 3600)  # 25 hours ago
    with pytest.raises(TokenExpiredException) as exc:
        enforce_iat(expired_iat)
    assert exc.value.body == {"error": "token_expired"}
    assert exc.value.status_code == 401


def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredException) as exc:
        enforce_iat(None)
    assert exc.value.body == {"error": "token_expired"}
    assert exc.value.status_code == 401
