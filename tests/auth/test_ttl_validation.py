import time
from fastapi import HTTPException
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat


def test_valid_recent_iat_passes():
    fixed_now = 1_600_000_000
    with freeze_time(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(fixed_now))):
        # iat 1 hour ago
        validate_iat(fixed_now - 3600)


def test_expired_iat_returns_401():
    fixed_now = 1_600_000_000
    with freeze_time(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(fixed_now))):
        iat = fixed_now - 90_000  # 25 hours ago
        try:
            validate_iat(iat)
            assert False, "Expected HTTPException for expired iat"
        except HTTPException as exc:
            assert exc.status_code == 401
            assert exc.detail == {"error": "token_expired"}


def test_missing_iat_returns_401():
    fixed_now = 1_600_000_000
    with freeze_time(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(fixed_now))):
        try:
            validate_iat(None)
            assert False, "Expected HTTPException for missing iat"
        except HTTPException as exc:
            assert exc.status_code == 401
            assert exc.detail == {"error": "token_expired"}
