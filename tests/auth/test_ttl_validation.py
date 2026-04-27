import pytest
from freezegun import freeze_time

from alfred_coo.auth.ttl_validator import validate_token, TokenExpiredError

FIXED_NOW = 1_700_000_000  # arbitrary Unix timestamp

@freeze_time("@FIXED_NOW")
def test_valid_recent_iat_passes():
    # Token issued 1 hour ago should be accepted.
    iat = FIXED_NOW - 3600
    assert validate_token(iat) is None

@freeze_time("@FIXED_NOW")
def test_expired_iat_returns_401():
    iat = FIXED_NOW - (25 * 3600)
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(iat)
    assert exc.value.body == "{\"error\":\"token_expired\"}"

@freeze_time("@FIXED_NOW")
def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(None)
    assert exc.value.body == "{\"error\":\"token_expired\"}"
