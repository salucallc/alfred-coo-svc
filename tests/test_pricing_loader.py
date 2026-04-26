import pytest
from alfred_coo.pricing import load

def test_openrouter_free_input():
    pricing = load()
    assert pricing["openrouter/free"]["input_per_1k"] == 0.0
