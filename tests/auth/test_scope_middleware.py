import json
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from alfred_coo.auth.scope_middleware import add_scope_middleware, requires_scope

# Mock token payload injection dependency
async def fake_token(payload: dict):
    # Dependency that would normally set request.state.token_payload
    async def dependency(request):
        request.state.token_payload = payload
        return payload
    return Depends(dependency)

app = FastAPI()
add_scope_middleware(app)

@app.get("/public")
async def public_endpoint():
    return {"msg": "public"}

@app.get("/protected")
@requires_scope("fleet:read")
async def protected_endpoint(token=Depends(fake_token({"scope": "fleet:read fleet:write"}))):
    return {"msg": "protected"}

@app.get("/missing_scope")
@requires_scope("fleet:write")
async def missing_scope_endpoint(token=Depends(fake_token({"scope": "fleet:read"})):
    return {"msg": "should not reach"}

@app.get("/no_scope_claim")
@requires_scope("fleet:read")
async def no_scope_claim_endpoint(token=Depends(fake_token({})):
    return {"msg": "should not reach"}

client = TestClient(app)

def test_scope_present_returns_200():
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json() == {"msg": "protected"}

def test_scope_missing_returns_403_with_payload():
    response = client.get("/missing_scope")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:write"}

def test_no_scope_claim_returns_403():
    response = client.get("/no_scope_claim")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}
