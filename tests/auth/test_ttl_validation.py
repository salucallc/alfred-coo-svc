import time
import pytest
from alfred_coo.auth.ttl_validator import check_iat

def test_valid_recent_iat_passes():
    recent = int(time.time()) - 3600  # 1 hour ago
    # should not raise
    assert check_iat(recent) is None

def test_expired_iat_returns_401():
    expired = int(time.time()) - 25 * 3600  # 25 hours ago
    with pytest.raises(ValueError) as exc:
        check_iat(expired)
    assert str(exc.value) == "{'error': 'token_expired'}"

def test_missing_iat_returns_401():
    with pytest.raises(ValueError) as exc:
        check_iat(None)
    assert str(exc.value) == "{'error': 'token_expired'}"
