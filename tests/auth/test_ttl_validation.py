import time
import pytest
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import validate_token, TokenExpiredError

@freeze_time("2026-04-28")
def test_valid_recent_iat_passes():
    payload = {"iat": int(time.time()) - 3600}  # 1 hour ago
    assert validate_token(payload) is True

@freeze_time("2026-04-28")
def test_expired_iat_returns_401():
    payload = {"iat": int(time.time()) - 25 * 3600}  # 25 hours ago
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert exc.value.status_code == 401
    assert exc.value.body == '{"error":"token_expired"}'

@freeze_time("2026-04-28")
def test_missing_iat_returns_401():
    payload = {}
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(payload)
    assert exc.value.status_code == 401
    assert exc.value.body == '{"error":"token_expired"}'
