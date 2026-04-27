import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_token, TokenExpiredError

@freeze_time("2024-01-01 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = {"iat": now - 3600}  # 1 hour ago
    # should not raise
    validate_token(payload)

@freeze_time("2024-01-01 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    payload = {"iat": now - 25 * 3600}  # 25 hours ago
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert str(exc.value) == "token_expired"

@freeze_time("2024-01-01 12:00:00")
def test_missing_iat_returns_401():
    payload = {}
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert str(exc.value) == "token_expired"
