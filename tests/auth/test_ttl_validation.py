import time
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import check_iat

def test_valid_recent_iat_passes():
    with freeze_time("2026-01-01 12:00:00"):
        now = int(time.time())
        iat = now - 3600  # 1 hour ago
        assert check_iat(iat) is None

def test_expired_iat_returns_401():
    with freeze_time("2026-01-01 12:00:00"):
        now = int(time.time())
        iat = now - 90000  # 25 hours ago
        assert check_iat(iat) == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with freeze_time("2026-01-01 12:00:00"):
        assert check_iat(None) == {"error": "token_expired"}
