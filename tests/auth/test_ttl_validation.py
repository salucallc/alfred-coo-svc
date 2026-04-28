import json
import time
from datetime import datetime
from freezegun import freeze_time
from alfred_coo.auth.ttl_validator import check_ttl

def test_valid_recent_iat_passes():
    frozen_now = datetime.utcfromtimestamp(1600000000)
    with freeze_time(frozen_now):
        iat = int(time.time()) - 3600  # 1 hour ago
        resp = check_ttl(iat)
        assert resp.status_code == 200

def test_expired_iat_returns_401():
    frozen_now = datetime.utcfromtimestamp(1600000000)
    with freeze_time(frozen_now):
        iat = int(time.time()) - 90000  # 25 hours ago
        resp = check_ttl(iat)
        assert resp.status_code == 401
        assert json.loads(resp.content) == {"error": "token_expired"}

def test_missing_iat_returns_401():
    frozen_now = datetime.utcfromtimestamp(1600000000)
    with freeze_time(frozen_now):
        resp = check_ttl(None)
        assert resp.status_code == 401
        assert json.loads(resp.content) == {"error": "token_expired"}
