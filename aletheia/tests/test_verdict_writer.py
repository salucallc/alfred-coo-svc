from fastapi.testclient import TestClient
from aletheia.app.main import app

client = TestClient(app)

def test_debug_verdict_endpoint():
    payload = {
        "verdict": "PASS",
        "verifier_model": "qwen3-coder:480b-cloud",
        "generator_model": "test-gen-model",
        "action_class": "test_action",
        "evidence_sha256": "abc123def456"
    }
    response = client.post("/v1/_debug/verdict", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "recorded"
    assert data["verdict"] == "PASS"
