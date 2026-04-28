import time
import pytest
import freezegun
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

def test_valid_recent_iat_passes():
    with freezegun.freeze_time("2023-01-01T12:00:00"):
        iat = int(time.time()) - 3600  # 1 hour ago
        # Should not raise
        validate_iat(iat)

def test_expired_iat_returns_401():
    with freezegun.freeze_time("2023-01-02T12:00:00"):
        iat = int(time.time()) - 90000  # 25 hours ago
        with pytest.raises(TokenExpiredError) as exc:
            validate_iat(iat)
        assert exc.value.body == {"error": "token_expired"}
        assert exc.value.status_code == 401

def test_missing_iat_returns_401():
    with freezegun.freeze_time("2023-01-01T12:00:00"):
        with pytest.raises(TokenExpiredError) as exc:
            validate_iat(None)
        assert exc.value.body == {"error": "token_expired"}
        assert exc.value.status_code == 401
