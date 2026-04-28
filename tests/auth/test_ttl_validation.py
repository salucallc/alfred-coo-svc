import time
import pytest
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import check_ttl

@freeze_time("2026-04-28 20:00:00")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    # Should not raise
    check_ttl(iat)

@freeze_time("2026-04-28 20:00:00")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - 90000  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        check_ttl(iat)
    assert str(exc.value) == '{"error":"token_expired"}'

@freeze_time("2026-04-28 20:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        check_ttl(None)
    assert str(exc.value) == '{"error":"token_expired"}'
