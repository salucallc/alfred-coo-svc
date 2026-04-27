import time
import pytest
from src.alfred_coo.auth.ttl_validator import validate_token


def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = {"iat": now - 3600}  # 1 hour ago
    status, body = validate_token(payload)
    assert status == 200
    assert body != {"error": "token_expired"}


def test_expired_iat_returns_401():
    now = int(time.time())
    payload = {"iat": now - 25 * 3600}  # 25 hours ago
    status, body = validate_token(payload)
    assert status == 401
    assert body == {"error": "token_expired"}


def test_missing_iat_returns_401():
    payload = {}
    status, body = validate_token(payload)
    assert status == 401
    assert body == {"error": "token_expired"}
