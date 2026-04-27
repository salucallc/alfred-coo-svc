import time
import pytest
from src.alfred_coo.auth.scoped_tokens import generate_scoped_token, token_is_valid

def test_token_scopes_allow_read_but_not_write():
    scopes = ["soul:memory:read"]
    token = generate_scoped_token(scopes, ttl_seconds=3600)
    assert token_is_valid(token, "soul:memory:read") is True
    # Simulate write scope check
    assert token_is_valid(token, "soul:memory:write") is False

def test_token_ttl_enforced():
    scopes = ["soul:memory:read"]
    token = generate_scoped_token(scopes, ttl_seconds=1)
    assert token_is_valid(token, "soul:memory:read") is True
    time.sleep(2)
    assert token_is_valid(token, "soul:memory:read") is False

def test_multiple_scopes():
    scopes = ["soul:memory:read", "tiresias:audit:read"]
    token = generate_scoped_token(scopes, ttl_seconds=3600)
    assert token_is_valid(token, "soul:memory:read") is True
    assert token_is_valid(token, "tiresias:audit:read") is True
