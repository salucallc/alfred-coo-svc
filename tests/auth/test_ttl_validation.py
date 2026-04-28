import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat


def test_valid_recent_iat_passes():
    with freeze_time("2026-04-28 12:00:00"):
        now = int(time.time())
        iat = now - 3600  # 1 hour ago
        assert validate_iat(iat) is None


def test_expired_iat_returns_401():
    with freeze_time("2026-04-28 12:00:00"):
        now = int(time.time())
        iat = now - 90000  # 25 hours ago
        assert validate_iat(iat) == {"error": "token_expired"}


def test_missing_iat_returns_401():
    with freeze_time("2026-04-28 12:00:00"):
        assert validate_iat(None) == {"error": "token_expired"}
