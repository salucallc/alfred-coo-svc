import time
import pytest
from alfred_coo.auth.ttl_validator import TokenExpiredError, validate_iat

def test_valid_recent_iat_passes(monkeypatch):
    fixed_now = 1_600_000_000
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    # iat one hour ago
    iat = fixed_now - 3600
    # should not raise
    validate_iat(iat)

def test_expired_iat_returns_401(monkeypatch):
    fixed_now = 1_600_000_000
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    iat = fixed_now - 90_000  # 25h
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(iat)
    assert exc.value.status_code == 401
    assert exc.value.body == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(None)
    assert exc.value.body == {"error": "token_expired"}
