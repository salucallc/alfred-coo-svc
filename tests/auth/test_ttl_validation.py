import json
import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat

@freeze_time("2026-01-01T00:00:00Z")
def test_valid_recent_iat_passes():
    iat = int(time.time()) - 3600  # 1 hour ago
    status, body = validate_iat({"iat": iat})
    assert status == 200
    assert json.loads(body) == {}

@freeze_time("2026-01-01T00:00:00Z")
def test_expired_iat_returns_401():
    iat = int(time.time()) - (25 * 3600)  # 25 hours ago
    status, body = validate_iat({"iat": iat})
    assert status == 401
    assert body == json.dumps({"error": "token_expired"})

@freeze_time("2026-01-01T00:00:00Z")
def test_missing_iat_returns_401():
    status, body = validate_iat({})
    assert status == 401
    assert body == json.dumps({"error": "token_expired"})
