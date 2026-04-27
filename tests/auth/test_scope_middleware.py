import base64
import json
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from src.alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope

app = FastAPI()
app.add_middleware(ScopeMiddleware)

@app.get("/protected")
def protected(dep: None = Depends(requires_scope("fleet:read"))):
    return {"ok": True}

def make_token(payload: dict) -> str:
    header = {"alg": "none"}
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64(header)}.{b64(payload)}."


def test_scope_present_returns_200():
    token = make_token({"scope": "fleet:read other:perm"})
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_scope_missing_returns_403_with_payload():
    token = make_token({"scope": "other:perm"})
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "insufficient_scope", "required": "fleet:read"}


def test_no_scope_claim_returns_403():
    token = make_token({})
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "insufficient_scope", "required": "fleet:read"}
