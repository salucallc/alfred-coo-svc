import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_iat
from httpx import HTTPStatusError

@freeze_time("2026-01-01 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    validate_iat(iat)

@freeze_time("2026-01-01 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - 25 * 3600  # 25 hours ago
    with pytest.raises(HTTPStatusError) as exc:
        validate_iat(iat)
    assert exc.value.response.json() == {"error": "token_expired"}

@freeze_time("2026-01-01 12:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(HTTPStatusError) as exc:
        validate_iat(None)
    assert exc.value.response.json() == {"error": "token_expired"}
