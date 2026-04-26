from fastapi.testclient import TestClient
from aletheia.app.preflight.server import app

client = TestClient(app)

def test_preflight_nonexistent_channel():
    response = client.post("/v1/preflight", json={"channel": "nonexistent"})
    assert response.status_code == 412
    assert response.json()["detail"].startswith("FAIL")

def test_preflight_valid_channel():
    response = client.post("/v1/preflight", json={"channel": "general"})
    assert response.status_code == 200
    assert response.json()["verdict"] == "PASS"
