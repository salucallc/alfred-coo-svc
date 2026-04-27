import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat_claim

@freeze_time("2026-01-01T12:00:00Z")
def test_valid_recent_iat_passes():
    token = {"iat": int(time.time()) - 3600}
    validate_iat_claim(token)

@freeze_time("2026-01-01T12:00:00Z")
def test_expired_iat_returns_401():
    token = {"iat": int(time.time()) - 25 * 3600}
    with pytest.raises(ValueError) as exc:
        validate_iat_claim(token)
    assert str(exc.value) == '{"error":"token_expired"}'

@freeze_time("2026-01-01T12:00:00Z")
def test_missing_iat_returns_401():
    token = {}
    with pytest.raises(ValueError) as exc:
        validate_iat_claim(token)
    assert str(exc.value) == '{"error":"token_expired"}'
