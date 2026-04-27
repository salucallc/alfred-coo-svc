import pytest
import time
from alfred_coo.auth.ttl_validator import validate_iat, TokenExpiredError

class FreezeTime:
    def __init__(self, frozen_ts):
        self.frozen_ts = frozen_ts
        self.original_time = time.time
    def __enter__(self):
        time.time = lambda: self.frozen_ts
    def __exit__(self, exc_type, exc, tb):
        time.time = self.original_time

def test_valid_recent_iat_passes():
    with FreezeTime(1_600_000_000):
        # 1 hour ago
        validate_iat(1_600_000_000 - 3600)

def test_expired_iat_returns_401():
    with FreezeTime(1_600_000_000):
        with pytest.raises(TokenExpiredError) as exc:
            # 25 hours ago
            validate_iat(1_600_000_000 - 25 * 3600)
        assert exc.value.args[0] == '{"error":"token_expired"}'

def test_missing_iat_returns_401():
    with FreezeTime(1_600_000_000):
        with pytest.raises(TokenExpiredError) as exc:
            validate_iat(None)
        assert exc.value.args[0] == '{"error":"token_expired"}'
