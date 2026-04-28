import time
import pytest
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import validate_iat

@freeze_time("2026-04-28 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    payload = {"iat": now - 3600}  # 1 hour ago
    assert validate_iat(payload) is None

@freeze_time("2026-04-28 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    payload = {"iat": now - 90000}  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        validate_iat(payload)
    assert str(exc.value) == "token_expired"

@freeze_time("2026-04-28 12:00:00")
def test_missing_iat_returns_401():
    payload = {}
    with pytest.raises(ValueError) as exc:
        validate_iat(payload)
    assert str(exc.value) == "token_expired"
