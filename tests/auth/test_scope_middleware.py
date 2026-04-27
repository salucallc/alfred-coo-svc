import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope

# Simple token injection middleware for testing purposes.
class TokenInjectMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        request.state.token = self.token
        return await call_next(request)


def create_app(token):
    app = FastAPI()
    app.add_middleware(TokenInjectMiddleware, token=token)
    app.add_middleware(ScopeMiddleware)

    @app.get("/protected")
    @requires_scope("fleet:read")
    async def protected_endpoint():
        return {"result": "ok"}

    return app


def test_scope_present_returns_200():
    app = create_app({"scope": "fleet:read fleet:write"})
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 200
    assert response.json() == {"result": "ok"}


def test_scope_missing_returns_403_with_payload():
    app = create_app({"scope": "fleet:write"})
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}


def test_no_scope_claim_returns_403():
    app = create_app({})
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}

