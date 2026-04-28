import json
import time
import pytest
from freezegun import freeze_time
from src.alfred_coo.auth.ttl_validator import validate_iat

FREEZE_TIME = "2023-01-01T12:00:00"

def _now_unix():
    return int(time.time())

@freeze_time(FREEZE_TIME)
def test_valid_recent_iat_passes():
    now = _now_unix()
    iat = now - 3600  # 1 hour ago
    # Should not raise
    validate_iat(iat)

@freeze_time(FREEZE_TIME)
def test_expired_iat_returns_401():
    now = _now_unix()
    iat = now - 25 * 3600  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        validate_iat(iat)
    assert str(exc.value) == json.dumps({"error": "token_expired"})

def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        validate_iat(None)
    assert str(exc.value) == json.dumps({"error": "token_expired"})
