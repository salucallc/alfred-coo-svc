import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_token_iat


def test_valid_recent_iat_passes():
    with freeze_time("2026-01-01T00:00:00Z"):
        now = int(time.time())
        token = {"iat": now - 3600}  # 1 hour ago
        status, _ = validate_token_iat(token)
        assert status == 200


def test_expired_iat_returns_401():
    with freeze_time("2026-01-01T00:00:00Z"):
        now = int(time.time())
        token = {"iat": now - 90000}  # 25 hours ago
        status, body = validate_token_iat(token)
        assert status == 401
        assert body == {"error": "token_expired"}


def test_missing_iat_returns_401():
    with freeze_time("2026-01-01T00:00:00Z"):
        token = {}
        status, body = validate_token_iat(token)
        assert status == 401
        assert body == {"error": "token_expired"}
