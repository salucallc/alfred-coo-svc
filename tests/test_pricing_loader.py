import pytest
from alfred_coo.pricing import load

def test_pricing_loader_returns_expected_value():
    pricing = load()
    assert "openrouter/free" in pricing, "Key 'openrouter/free' missing in pricing data"
    assert pricing["openrouter/free"]["input_per_1k"] == 0.0
