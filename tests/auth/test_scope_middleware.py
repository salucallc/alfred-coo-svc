'''python
"""Tests for the scope‑enforcement middleware (OPS‑14c).

The tests construct a minimal FastAPI app, install a dummy upstream
middleware that injects a ``auth_payload`` onto ``request.state``, and then
mount the :class:`~alfred_coo.auth.scope_middleware.ScopeMiddleware`.
Three scenarios are exercised:

1. The required scope is present – the endpoint returns ``200``.
2. The required scope is missing – the endpoint returns ``403`` with the
   exact JSON error payload required by the APE/V spec.
3. No ``scope`` claim at all – also results in ``403``.
"""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from alfred_coo.auth.scope_middleware import ScopeMiddleware, requires_scope

# ---------------------------------------------------------------------------
# Helper upstream middleware that pretends the token validator has run and
# stores a payload on ``request.state.auth_payload``.
# ---------------------------------------------------------------------------

class DummyAuthMiddleware:
    def __init__(self, app, payload):
        self.app = app
        self.payload = payload

    async def __call__(self, scope, receive, send):
        # FastAPI creates a Request object later; we only need to stash the
        # payload on the ASGI ``scope``'s ``state``‑like dict.
        # ``scope`` does not have ``state`` by default, so we create a mutable
        # attribute container.
        if "state" not in scope:
            scope["state"] = type("State", (), {})()
        setattr(scope["state"], "auth_payload", self.payload)
        await self.app(scope, receive, send)

# ---------------------------------------------------------------------------
# Build a FastAPI app used by the three test cases.
# ---------------------------------------------------------------------------

def make_app(test_payload):
    app = FastAPI()
    # Insert dummy auth before the scope middleware.
    app.add_middleware(DummyAuthMiddleware, payload=test_payload)
    app.add_middleware(ScopeMiddleware)

    @app.get("/demo")
    async def demo_route(dep: None = Depends(requires_scope("fleet:read")):
        return {"msg": "ok"}

    return app

# ---------------------------------------------------------------------------
# Test 1 – required scope present.
# ---------------------------------------------------------------------------

def test_scope_present_returns_200():
    payload = {"scope": "fleet:read other:something"}
    client = TestClient(make_app(payload))
    response = client.get("/demo")
    assert response.status_code == 200
    assert response.json() == {"msg": "ok"}

# ---------------------------------------------------------------------------
# Test 2 – required scope missing.
# ---------------------------------------------------------------------------

def test_scope_missing_returns_403_with_payload():
    payload = {"scope": "other:something"}
    client = TestClient(make_app(payload))
    response = client.get("/demo")
    assert response.status_code == 403
    # The body must match the exact JSON structure required by the APE/V.
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}

# ---------------------------------------------------------------------------
# Test 3 – no scope claim at all.
# ---------------------------------------------------------------------------

def test_no_scope_claim_returns_403():
    payload = {}  # No ``scope`` or ``scopes`` key.
    client = TestClient(make_app(payload))
    response = client.get("/demo")
    assert response.status_code == 403
    assert response.json() == {"error": "insufficient_scope", "required": "fleet:read"}
'''