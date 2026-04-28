import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

def test_valid_recent_iat_passes(monkeypatch):
    fake_now = 1600000000
    monkeypatch.setattr(time, "time", lambda: fake_now)
    iat = fake_now - 3600  # 1 hour ago
    valid, err = validate_iat(iat)
    assert valid
    assert err == {}

def test_expired_iat_returns_401(monkeypatch):
    fake_now = 1600000000
    monkeypatch.setattr(time, "time", lambda: fake_now)
    iat = fake_now - 25 * 3600  # 25 hours ago
    valid, err = validate_iat(iat)
    assert not valid
    assert err == {"error": "token_expired"}

def test_missing_iat_returns_401():
    valid, err = validate_iat(None)
    assert not valid
    assert err == {"error": "token_expired"}
