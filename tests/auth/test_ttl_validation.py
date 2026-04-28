import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat, TOKEN_EXPIRED_BODY

EXPECTED = TOKEN_EXPIRED_BODY

@freeze_time("2026-04-28T00:00:00Z")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    assert validate_iat(iat) == {}

@freeze_time("2026-04-28T00:00:00Z")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - 25 * 3600  # 25 hours ago
    assert validate_iat(iat) == EXPECTED

@freeze_time("2026-04-28T00:00:00Z")
def test_missing_iat_returns_401():
    assert validate_iat(None) == EXPECTED
