import time
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

@pytest.fixture(autouse=True)
def freeze_time(monkeypatch):
    # Freeze time to a known timestamp
    fixed = 1_600_000_000  # arbitrary epoch seconds
    monkeypatch.setattr(time, "time", lambda: fixed)
    return fixed

def test_valid_recent_iat_passes(freeze_time):
    # iat = now - 1 hour
    iat = int(freeze_time) - 3600
    status, body = validate_iat(iat)
    assert status == 200
    assert body == {}

def test_expired_iat_returns_401(freeze_time):
    # iat = now - 25 hours
    iat = int(freeze_time) - (25 * 3600)
    status, body = validate_iat(iat)
    assert status == 401
    assert body == {"error": "token_expired"}

def test_missing_iat_returns_401(freeze_time):
    status, body = validate_iat(None)
    assert status == 401
    assert body == {"error": "token_expired"}
