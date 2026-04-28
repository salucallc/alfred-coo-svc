import json
import time
import pytest
from fastapi import HTTPException, status
from freezegun import freeze_time

from alfred_coo.auth.ttl_validator import enforce_ttl, TOKEN_EXPIRED_BODY

@freeze_time("2026-01-01 12:00:00")
def test_valid_recent_iat_passes():
    iat = int(time.time()) - 3600  # 1 hour ago
    # Should not raise
    enforce_ttl(iat)

@freeze_time("2026-01-01 12:00:00")
def test_expired_iat_returns_401():
    iat = int(time.time()) - 25 * 3600  # 25 hours ago
    with pytest.raises(HTTPException) as exc:
        enforce_ttl(iat)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.content == json.dumps(TOKEN_EXPIRED_BODY)

@freeze_time("2026-01-01 12:00:00")
def test_missing_iat_returns_401():
    with pytest.raises(HTTPException) as exc:
        enforce_ttl(None)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.content == json.dumps(TOKEN_EXPIRED_BODY)
