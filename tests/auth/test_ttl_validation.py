import pytest
import time
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError


def test_valid_recent_iat_passes(monkeypatch):
    """A token issued 1 hour ago should be accepted."""
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    iat = now - 3600  # 1 hour ago
    # Should not raise an exception
    validate_iat(iat)


def test_expired_iat_returns_401(monkeypatch):
    """A token older than 24 hours should raise TokenExpiredError."""
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    iat = now - 90_000  # 25 hours ago (exceeds 86400)
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(iat)
    assert str(exc.value) == "{\"error\":\"token_expired\"}"


def test_missing_iat_returns_401(monkeypatch):
    """A missing `iat` claim should raise TokenExpiredError."""
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(None)
    assert str(exc.value) == "{\"error\":\"token_expired\"}"
