import pytest
from alfred_coo.pricing import load_pricing

def test_pricing_loader():
    pricing = load_pricing()
    # Verify free tier exists and values are zero
    assert "free" in pricing, "free tier missing"
    free = pricing["free"]
    assert free.get("input_per_1k") == 0.0
    assert free.get("output_per_1k") == 0.0
    # Verify a paid provider example exists
    assert "openrouter" in pricing, "openrouter provider missing"
