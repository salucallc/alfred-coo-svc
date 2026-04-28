import time
import pytest
from fastapi import HTTPException
from src.alfred_coo.auth.ttl_validator import enforce_ttl

def test_valid_recent_iat_passes(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    # Should not raise any exception
    enforce_ttl(now - 3600)  # 1 hour ago

def test_expired_iat_returns_401(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with pytest.raises(HTTPException) as exc:
        enforce_ttl(now - 90000)  # 25 hours ago (>24h)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with pytest.raises(HTTPException) as exc:
        enforce_ttl(None)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"error": "token_expired"}
