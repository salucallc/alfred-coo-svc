import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat


def test_valid_recent_iat_passes():
    # Freeze time at a known point
    with freeze_time("2023-01-01 12:00:00"):
        now = int(time.time())
        recent_iat = now - 3600  # 1 hour ago
        status, body = validate_iat(recent_iat)
        assert status == 200
        assert body == {}


def test_expired_iat_returns_401():
    with freeze_time("2023-01-01 12:00:00"):
        now = int(time.time())
        expired_iat = now - (25 * 3600)  # 25 hours ago
        status, body = validate_iat(expired_iat)
        assert status == 401
        assert body == {"error": "token_expired"}


def test_missing_iat_returns_401():
    with freeze_time("2023-01-01 12:00:00"):
        status, body = validate_iat(None)
        assert status == 401
        assert body == {"error": "token_expired"}
