import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

FIXED_TIME = 1_600_000_000  # arbitrary fixed timestamp for deterministic tests

@pytest.fixture(autouse=True)
def freeze_time(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: FIXED_TIME)

def test_valid_recent_iat_passes():
    recent_iat = FIXED_TIME - 3600  # 1 hour ago
    assert validate_iat(recent_iat) == {}

def test_expired_iat_returns_401():
    expired_iat = FIXED_TIME - (25 * 3600)  # 25 hours ago
    assert validate_iat(expired_iat) == {"error": "token_expired"}

def test_missing_iat_returns_401():
    assert validate_iat(None) == {"error": "token_expired"}
