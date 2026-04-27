import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import enforce_ttl
import httpx

def test_valid_recent_iat_passes():
    with freeze_time("2026-01-01T00:00:00Z"):
        now = int(time.time())
        # iat 1 hour ago should not raise
        enforce_ttl(now - 3600)

def test_expired_iat_returns_401():
    with freeze_time("2026-01-01T00:00:00Z"):
        now = int(time.time())
        with pytest.raises(httpx.HTTPStatusError) as exc:
            enforce_ttl(now - 25 * 3600)
        assert exc.value.response.json() == {"error": "token_expired"}

def test_missing_iat_returns_401():
    with freeze_time("2026-01-01T00:00:00Z"):
        with pytest.raises(httpx.HTTPStatusError) as exc:
            enforce_ttl(None)
        assert exc.value.response.json() == {"error": "token_expired"}
