import json
import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope

# Helper middleware to inject a token payload for testing
class DummyTokenMiddleware:
    def __init__(self, app, token_payload):
        self.app = app
        self.token_payload = token_payload

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request
        request = Request(scope, receive=receive)
        request.state.token = self.token_payload
        await self.app(scope, receive, send)

def build_app(token_payload: dict | None):
    app = FastAPI()
    # Inject dummy token payload before scope middleware
    app.add_middleware(DummyTokenMiddleware, token_payload=token_payload)
    app.add_middleware(ScopeMiddleware)

    @app.get("/public")
    async def public():
        return {"msg": "public"}

    @app.get("/protected")
    @requires_scope("fleet:read")
    async def protected():
        return {"msg": "protected"}

    return app

client = TestClient(build_app({"scope": "fleet:read other:scope"}))

def test_scope_present_returns_200():
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json() == {"msg": "protected"}

client_missing = TestClient(build_app({"scope": "other:scope"}))

def test_scope_missing_returns_403_with_payload():
    response = client_missing.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}

client_no_claim = TestClient(build_app({}))

def test_no_scope_claim_returns_403():
    response = client_no_claim.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}
