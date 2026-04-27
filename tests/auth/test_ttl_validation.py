import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_ttl
from fastapi import HTTPException

@freeze_time("2026-01-01T00:00:00Z")
def test_valid_recent_iat_passes():
    iat = int(time.time()) - 3600  # 1 hour ago
    # Should not raise
    validate_ttl(iat)

@freeze_time("2026-01-01T00:00:00Z")
def test_expired_iat_returns_401():
    iat = int(time.time()) - 25 * 3600  # 25 hours ago
    with pytest.raises(HTTPException) as exc:
        validate_ttl(iat)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"error": "token_expired"}

@freeze_time("2026-01-01T00:00:00Z")
def test_missing_iat_returns_401():
    with pytest.raises(HTTPException) as exc:
        validate_ttl(None)
    assert exc.value.status_code == 401
    assert exc.value.detail == {"error": "token_expired"}
