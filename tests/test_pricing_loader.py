from alfred_coo.pricing import load

def test_pricing_load():
    data = load()
    assert isinstance(data, dict)
    assert "ollama_max" in data
    assert data["ollama_max"]["flat_monthly_usd"] == 100
