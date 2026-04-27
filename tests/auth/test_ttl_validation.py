import pytest
import time
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import validate_scoped_token, TokenExpiredError
import base64
import json

def _make_token(iat: int = None) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {}
    if iat is not None:
        payload["iat"] = iat
    def _b64url_encode(obj):
        json_str = json.dumps(obj, separators=(',', ':'))
        b64 = base64.urlsafe_b64encode(json_str.encode()).decode().rstrip('=')
        return b64
    token = f"{_b64url_encode(header)}.{_b64url_encode(payload)}."
    return token

@freeze_time("2023-01-01T00:00:00Z")
def test_valid_recent_iat_passes():
    now = int(time.time())
    iat = now - 3600  # 1 hour ago
    token = _make_token(iat)
    # Should not raise
    validate_scoped_token(token)

@freeze_time("2023-01-01T00:00:00Z")
def test_expired_iat_returns_401():
    now = int(time.time())
    iat = now - (25 * 3600)  # 25 hours ago
    token = _make_token(iat)
    with pytest.raises(TokenExpiredError) as exc:
        validate_scoped_token(token)
    assert exc.value.message == '{"error":"token_expired"}'

@freeze_time("2023-01-01T00:00:00Z")
def test_missing_iat_returns_401():
    token = _make_token()  # no iat claim
    with pytest.raises(TokenExpiredError) as exc:
        validate_scoped_token(token)
    assert exc.value.message == '{"error":"token_expired"}'
