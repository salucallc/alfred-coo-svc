import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat, TokenValidationError

@freeze_time("2026-04-27 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = {"iat": now - 3600}  # 1h ago
    # should not raise
    validate_iat(payload)

@freeze_time("2026-04-27 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    payload = {"iat": now - 90_000}  # >24h ago
    with pytest.raises(TokenValidationError) as exc:
        validate_iat(payload)
    assert str(exc.value) == "expired"

@freeze_time("2026-04-27 12:00:00")
def test_missing_iat_returns_401():
    payload = {}
    with pytest.raises(TokenValidationError) as exc:
        validate_iat(payload)
    assert str(exc.value) == "missing iat"
