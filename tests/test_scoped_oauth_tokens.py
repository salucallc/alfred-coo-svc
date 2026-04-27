import pytest
from alfred_coo.auth.scoped_tokens import get_token, can_read_memory, can_write_memory

@pytest.fixture(scope="module")
def read_token():
    return get_token(["soul:memory:read"])

def test_read_allowed(read_token):
    assert can_read_memory(read_token) is True

def test_write_forbidden(read_token):
    assert can_write_memory(read_token) is False
