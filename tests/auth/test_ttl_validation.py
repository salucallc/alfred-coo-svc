import time
import json
import pytest
from alfred_coo.auth.ttl_validator import validate_iat

def test_valid_recent_iat_passes(monkeypatch):
    fixed_now = 1_600_000_000
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    iat = fixed_now - 3600  # 1 hour ago
    # Should not raise any exception
    validate_iat(iat)

def test_expired_iat_returns_401(monkeypatch):
    fixed_now = 1_600_000_000
    monkeypatch.setattr(time, "time", lambda: fixed_now)
    iat = fixed_now - (25 * 3600)  # 25 hours ago, beyond TTL
    with pytest.raises(ValueError) as exc:
        validate_iat(iat)
    assert str(exc.value) == json.dumps({"error": "token_expired"})

def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        validate_iat(None)
    assert str(exc.value) == json.dumps({"error": "token_expired"})
