import time
import pytest
import httpx
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import enforce_ttl

@freeze_time("2026-01-01 12:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago, within TTL
    # Should not raise any exception
    enforce_ttl(iat)

@freeze_time("2026-01-01 12:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - 86400 - 3600  # 25 hours ago, exceeds TTL
    with pytest.raises(httpx.HTTPStatusError) as exc:
        enforce_ttl(iat)
    assert exc.value.response.status_code == 401
    assert exc.value.response.json() == {"error": "token_expired"}

@freeze_time("2026-01-01 12:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(httpx.HTTPStatusError) as exc:
        enforce_ttl(None)
    assert exc.value.response.status_code == 401
    assert exc.value.response.json() == {"error": "token_expired"}
