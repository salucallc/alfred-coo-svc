import pytest
from alfred_coo.pricing import load

def test_pricing_loader():
    pricing = load()
    # Verify that free tier pricing returns 0.0 for input per 1k tokens
    assert pricing["free"]["input_per_1k"] == 0.0
    # Verify that Ollama Max flat monthly price is present
    assert pricing["ollama_max"]["flat_monthly_usd"] == 100