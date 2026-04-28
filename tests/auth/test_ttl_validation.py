import time
import pytest
from fastapi import HTTPException
from src.alfred_coo.auth.ttl_validator import enforce_ttl

def test_valid_recent_iat_passes():
    now = int(time.time())
    enforce_ttl(now - 3600)  # should not raise

def test_expired_iat_returns_401():
    from freezegun import freeze_time
    with freeze_time("2023-01-01T00:00:00"):
        now = int(time.time())
        expired_iat = now - 90000  # 25h
        with pytest.raises(HTTPException) as exc:
            enforce_ttl(expired_iat)
        assert exc.value.status_code == 401
        assert exc.value.detail == {"error":"token_expired"}

def test_missing_iat_returns_401():
    with pytest.raises(HTTPException) as exc:
        enforce_ttl(None)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"error":"token_expired"}
