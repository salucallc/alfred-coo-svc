import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope

app = FastAPI()
app.add_middleware(ScopeMiddleware)

# Helper middleware for the tests that injects scopes from a header.
@app.middleware("http")
async def inject_scopes(request, call_next):
    header = request.headers.get("x-scopes")
    if header:
        request.state.scopes = set(header.split(","))
    else:
        request.state.scopes = set()
    return await call_next(request)

@app.get("/protected")
async def protected(allowed=requires_scope("fleet:read")):
    return {"ok": True}

client = TestClient(app)


def test_scope_present_returns_200():
    response = client.get("/protected", headers={"x-scopes": "fleet:read,other:scope"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_scope_missing_returns_403_with_payload():
    response = client.get("/protected", headers={"x-scopes": "other:scope"})
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}


def test_no_scope_claim_returns_403():
    response = client.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}
