"""Tests for the TTL validator.

The tests freeze time to a known point and verify that:

* a recent ``iat`` (within 24 hours) passes without error;
* an ``iat`` older than 24 hours triggers a ``401`` with the required JSON;
* a missing ``iat`` also triggers the same ``401`` response.
"""

import time

import httpx
from freezegun import freeze_time

from src.alfred_coo.auth.ttl_validator import validate_iat


def test_valid_recent_iat_passes():
    with freeze_time("2026-04-28T12:00:00"):
        now = int(time.time())
        iat = now - 3600  # 1 hour ago
        # Should not raise
        assert validate_iat(iat) is None


def test_expired_iat_returns_401():
    with freeze_time("2026-04-28T12:00:00"):
        now = int(time.time())
        iat = now - 90000  # > 24h ago
        try:
            validate_iat(iat)
            assert False, "Expected HTTPStatusError"
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 401
            assert exc.response.json() == {"error": "token_expired"}


def test_missing_iat_returns_401():
    with freeze_time("2026-04-28T12:00:00"):
        try:
            validate_iat(None)
            assert False, "Expected HTTPStatusError"
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 401
            assert exc.response.json() == {"error": "token_expired"}
