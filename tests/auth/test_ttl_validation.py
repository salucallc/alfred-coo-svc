import time
import json
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import validate_iat
from flask import Response

@freeze_time("2026-04-27T00:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    assert validate_iat(iat) is None

@freeze_time("2026-04-27T00:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - 25 * 3600  # 25 hours ago
    resp = validate_iat(iat)
    assert isinstance(resp, Response)
    assert resp.status_code == 401
    assert json.loads(resp.get_data(as_text=True)) == {"error": "token_expired"}

@freeze_time("2026-04-27T00:00:00")
def test_missing_iat_returns_401():
    resp = validate_iat(None)
    assert isinstance(resp, Response)
    assert resp.status_code == 401
    assert json.loads(resp.get_data(as_text=True)) == {"error": "token_expired"}
