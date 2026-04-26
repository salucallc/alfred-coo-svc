import os
from alfred_coo.fleet_endpoint.memory_pull import pull_memory

def test_pull_memory_returns_dict(monkeypatch):
    # Mock requests.get to avoid real HTTP calls
    class DummyResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"batch": [], "next_global_seq": 0}
    def mock_get(*args, **kwargs):
        return DummyResponse()
    monkeypatch.setattr("alfred_coo.fleet_endpoint.memory_pull.requests.get", mock_get)
    os.environ["FLEET_HUB_URL"] = "http://dummy"
    os.environ["API_KEY"] = "testkey"
    result = pull_memory()
    assert isinstance(result, dict)
    assert "batch" in result
    assert "next_global_seq" in result
