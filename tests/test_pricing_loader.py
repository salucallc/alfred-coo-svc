import pytest
from alfred_coo.pricing import load

def test_pricing_load():
    pricing = load()
    # Verify free tier pricing for OpenRouter
    assert pricing["openrouter"]["free"]["input_per_1k"] == 0.0
    # Verify Ollama max flat monthly price exists
    assert pricing["ollama"]["max"]["flat_monthly_usd"] == 100
