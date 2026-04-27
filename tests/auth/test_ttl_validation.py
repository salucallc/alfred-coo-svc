import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat

@freeze_time("2023-01-01")
def test_valid_recent_iat_passes():
    now = int(time.time())
    token = {"iat": now - 3600}  # 1 hour ago
    status, body = validate_iat(token)
    assert status == 200

@freeze_time("2023-01-01")
def test_expired_iat_returns_401():
    now = int(time.time())
    token = {"iat": now - 86400 - 1}  # just over 24h ago
    status, body = validate_iat(token)
    assert status == 401
    assert body == {"error": "token_expired"}

@freeze_time("2023-01-01")
def test_missing_iat_returns_401():
    token = {}
    status, body = validate_iat(token)
    assert status == 401
    assert body == {"error": "token_expired"}
