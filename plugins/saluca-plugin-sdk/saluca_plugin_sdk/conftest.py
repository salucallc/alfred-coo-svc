import os
import pytest

@pytest.fixture
def soulkey():
    return os.getenv("SOULKEY", "testkey")
