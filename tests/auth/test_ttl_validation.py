import time
import pytest
import httpx
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import enforce_ttl

@freeze_time("2026-01-01")
def test_valid_recent_iat_passes():
    now = int(time.time())
    # 1 hour ago – should not raise
    enforce_ttl(now - 3600)

@freeze_time("2026-01-01")
def test_expired_iat_returns_401():
    now = int(time.time())
    with pytest.raises(httpx.HTTPStatusError) as exc:
        enforce_ttl(now - 90000)  # 25 hours ago
    assert exc.value.response.status_code == 401
    assert exc.value.response.json() == {"error": "token_expired"}

@freeze_time("2026-01-01")
def test_missing_iat_returns_401():
    with pytest.raises(httpx.HTTPStatusError) as exc:
        enforce_ttl(None)
    assert exc.value.response.status_code == 401
    assert exc.value.response.json() == {"error": "token_expired"}
