from freezegun import freeze_time
import time
from alfred_coo.auth.ttl_validator import validate_iat

@freeze_time("2026-04-28")
def test_valid_recent_iat_passes():
    now = int(time.time())
    status, _ = validate_iat(now - 3600)  # 1 hour ago
    assert status == 200

@freeze_time("2026-04-28")
def test_expired_iat_returns_401():
    now = int(time.time())
    status, body = validate_iat(now - 90000)  # 25 hours ago
    assert status == 401
    assert body == {"error": "token_expired"}

@freeze_time("2026-04-28")
def test_missing_iat_returns_401():
    status, body = validate_iat(None)
    assert status == 401
    assert body == {"error": "token_expired"}
