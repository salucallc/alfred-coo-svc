import time
import pytest
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import validate_iat

@freeze_time("2026-01-01 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    recent_iat = now - 3600  # 1 hour ago
    # Should not raise
    validate_iat(recent_iat)

@freeze_time("2026-01-01 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    expired_iat = now - 25 * 3600  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        validate_iat(expired_iat)
    assert str(exc.value) == '{"error":"token_expired"}'

@freeze_time("2026-01-01 12:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        validate_iat(None)
    assert str(exc.value) == '{"error":"token_expired"}'
