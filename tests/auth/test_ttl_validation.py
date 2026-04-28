import time
from fastapi import HTTPException
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_token_iat

def _make_token(iat: int | None):
    token = {}
    if iat is not None:
        token["iat"] = iat
    return token

@freeze_time("2026-05-01T12:00:00Z")
def test_valid_recent_iat_passes():
    now = int(time.time())
    token = _make_token(now - 3600)
    validate_token_iat(token)

@freeze_time("2026-05-01T12:00:00Z")
def test_expired_iat_returns_401():
    now = int(time.time())
    token = _make_token(now - 25 * 3600)
    try:
        validate_token_iat(token)
    except HTTPException as exc:
        assert exc.status_code == 401
        assert exc.detail == {"error": "token_expired"}
    else:
        assert False, "Expected HTTPException for expired token"

@freeze_time("2026-05-01T12:00:00Z")
def test_missing_iat_returns_401():
    token = _make_token(None)
    try:
        validate_token_iat(token)
    except HTTPException as exc:
        assert exc.status_code == 401
        assert exc.detail == {"error": "token_expired"}
    else:
        assert False, "Expected HTTPException for missing iat"
