import pytest
import json
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

@freeze_time("2026-01-01T12:00:00Z")
def test_valid_recent_iat_passes():
    # iat 1 hour ago
    iat = 1672573200  # corresponds to 2026-01-01T11:00:00Z
    # Should not raise
    validate_iat(iat)

@freeze_time("2026-01-01T12:00:00Z")
def test_expired_iat_returns_401():
    # iat 25 hours ago (expired)
    iat = 1672486800  # 2025-12-31T11:00:00Z
    with pytest.raises(TokenExpiredError) as exc_info:
        validate_iat(iat)
    assert json.loads(str(exc_info.value)) == {"error": "token_expired"}

@freeze_time("2026-01-01T12:00:00Z")
def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc_info:
        validate_iat(None)
    assert json.loads(str(exc_info.value)) == {"error": "token_expired"}
