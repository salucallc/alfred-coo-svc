import time
from src.alfred_coo.auth.ttl_validator import validate_iat


def test_valid_recent_iat_passes(monkeypatch):
    """A token issued less than an hour ago should be accepted."""
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    status, _ = validate_iat(iat)
    assert status == 200


def test_expired_iat_returns_401(monkeypatch):
    """A token older than 24 hours must be rejected with the exact error."""
    now = int(time.time())
    iAT = now - 90000  # 25 hours ago (> 86400)
    status, body = validate_iat(iAT)
    assert status == 401
    assert body == {"error": "token_expired"}


def test_missing_iat_returns_401():
    """Missing ``iat`` claim is denied by default."""
    status, body = validate_iat(None)
    assert status == 401
    assert body == {"error": "token_expired"}
