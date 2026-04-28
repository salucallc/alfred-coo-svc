import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

@freeze_time("2026-01-01T00:00:00Z")
def test_valid_recent_iat_passes():
    # iat 1 hour ago
    iat = int(time.time()) - 3600
    # Should not raise
    validate_iat(iat)

@freeze_time("2026-01-01T00:00:00Z")
def test_expired_iat_returns_401():
    # iat 25 hours ago
    iat = int(time.time()) - 25 * 3600
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(iat)
    assert str(exc.value) == "token_expired"

@freeze_time("2026-01-01T00:00:00Z")
def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc:
        validate_iat(None)
    assert str(exc.value) == "token_expired"
