from fastapi.testclient import TestClient
from aletheia.app.main import app

client = TestClient(app)

def test_debug_verdict():
    response = client.post("/v1/_debug/verdict", json={"verdict": "PASS"})
    assert response.status_code == 200
    assert response.json() == {"result": "written"}
