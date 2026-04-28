import json
import pytest
import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import enforce_ttl, TokenExpiredError

@freeze_time("2026-04-28T00:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    # Should not raise any exception for recent iat (1 hour ago)
    enforce_ttl(now - 3600)

@freeze_time("2026-04-28T00:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    with pytest.raises(TokenExpiredError) as exc:
        enforce_ttl(now - 90000)  # 25 hours ago, exceeds 24h TTL
    assert str(exc.value) == json.dumps({"error": "token_expired"})

@freeze_time("2026-04-28T00:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(TokenExpiredError) as exc:
        enforce_ttl(None)
    assert str(exc.value) == json.dumps({"error": "token_expired"})
