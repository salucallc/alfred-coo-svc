import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpired, ERROR_RESPONSE


def _assert_token_expired(exc: TokenExpired):
    assert isinstance(exc, TokenExpired)
    # Confirm the error payload matches the required response
    assert ERROR_RESPONSE == {"error": "token_expired"}


def test_valid_recent_iat_passes():
    # Token issued 1 hour ago should be accepted
    now = int(time.time())
    iat = now - 3600
    # No exception expected
    validate_iat(iat)


def test_expired_iat_returns_401():
    # Freeze time to a known point for reproducibility
    with freeze_time("2026-01-01 12:00:00"):
        now = int(time.time())
        iat = now - 86401  # just beyond 24h
        with pytest.raises(TokenExpired) as excinfo:
            validate_iat(iat)
        _assert_token_expired(excinfo.value)


def test_missing_iat_returns_401():
    with pytest.raises(TokenExpired) as excinfo:
        validate_iat(None)
    _assert_token_expired(excinfo.value)
