"""Tests for scope-based authorization middleware."""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from src.alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope


@pytest.fixture
def app():
    """Create a FastAPI app with scope middleware for testing."""
    test_app = FastAPI()
    test_app.add_middleware(ScopeMiddleware)
    
    @test_app.get("/protected/read")
    async def protected_read(required_scope=requires_scope("soul:memory:read")):
        return {"message": "read authorized"}
    
    @test_app.get("/protected/write")
    async def protected_write(required_scope=requires_scope("soul:memory:write")):
        return {"message": "write authorized"}
        
    @test_app.get("/public")
    async def public_route():
        return {"message": "public"}
    
    return test_app


@pytest.fixture
def client(app):
    """Create test client with scope injection helper."""
    
    class ScopeClient(TestClient):
        def request(self, *args, **kwargs):
            # Inject scopes into request state via custom header (middleware would extract from JWT)
            scopes = kwargs.pop("scopes", None)
            if scopes is not None:
                # Store in extra kwargs for ASGI scope modification
                kwargs["headers"] = kwargs.get("headers", {})
                kwargs["headers"]["x-test-scopes"] = " ".join(scopes) if isinstance(scopes, list) else scopes
            return super().request(*args, **kwargs)
    
    # Wrap the app to extract scopes from header and set in state
    class ScopeInjectionMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            scope_header = request.headers.get("x-test-scopes", "")
            if scope_header:
                request.state.scopes = scope_header
            return await call_next(request)
    
    app.add_middleware(ScopeInjectionMiddleware)
    return ScopeClient(app)


def test_scope_present_returns_200(client):
    """Test that valid scope returns 200 OK."""
    response = client.get("/protected/read", scopes=["soul:memory:read"])
    assert response.status_code == 200
    assert response.json() == {"message": "read authorized"}


def test_scope_missing_returns_403_with_payload(client):
    """Test that missing scope returns 403 with exact error payload."""
    response = client.get("/protected/read", scopes=["soul:memory:write"])  # Wrong scope
    assert response.status_code == 403
    
    expected_body = {"error": "insufficient_scope", "required": "soul:memory:read"}
    assert response.json() == expected_body


def test_no_scope_claim_returns_403(client):
    """Test that absent scope claim is treated as empty set (deny-by-default)."""
    response = client.get("/protected/read")  # No scopes provided
    assert response.status_code == 403
    
    expected_body = {"error": "insufficient_scope", "required": "soul:memory:read"}
    assert response.json() == expected_body


def test_multiple_scopes_one_granted_returns_200(client):
    """Test that having the required scope among many grants access."""
    response = client.get("/protected/read", scopes=["soul:memory:write", "soul:memory:read", "admin"])
    assert response.status_code == 200
    assert response.json() == {"message": "read authorized"}


def test_public_route_no_scope_required(client):
    """Test that public routes without scope requirements pass through."""
    response = client.get("/public")
    assert response.status_code == 200
    assert response.json() == {"message": "public"}
