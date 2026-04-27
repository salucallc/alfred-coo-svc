import time
import pytest
from alfred_coo.auth.ttl_validator import validate_token, TokenExpiredError, TTL_SECONDS

# Helper to create a token dict with given iat offset (seconds from now)
def make_token(iat_offset_seconds: int | None):
    if iat_offset_seconds is None:
        return {}
    return {'iat': int(time.time()) + iat_offset_seconds}

def test_valid_recent_iat_passes():
    # iat set to 1 hour ago (negative offset)
    token = make_token(iat_offset_seconds=-3600)
    validate_token(token)

def test_expired_iat_returns_401():
    token = make_token(iat_offset_seconds=-(TTL_SECONDS + 3600))
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(token)
    assert exc.value.status_code == 401
    assert exc.value.body == '{"error":"token_expired"}'

def test_missing_iat_returns_401():
    token = {}
    with pytest.raises(TokenExpiredError) as exc:
        validate_token(token)
    assert exc.value.status_code == 401
    assert exc.value.body == '{"error":"token_expired"}'
