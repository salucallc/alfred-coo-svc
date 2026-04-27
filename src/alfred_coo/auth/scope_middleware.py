import json
from typing import List, Set, Optional

from fastapi import Request, FastAPI
from fastapi.responses import JSONResponse

# Helper decorator to declare required scope on endpoint functions
def requires_scope(scope: str):
    """Attach a required scope attribute to a FastAPI route handler."""
    def decorator(func):
        setattr(func, "_required_scope", scope)
        return func
    return decorator

class ScopeMiddleware:
    """FastAPI/Starlette middleware that checks OAuth2 token scopes.

    Expected upstream authentication middleware to store the decoded JWT payload
    on ``request.state.token`` as a ``dict`` containing either a ``scope`` or
    ``scopes`` claim. The claim value is space‑delimited per RFC 8693.
    """

    def __init__(self, app: FastAPI):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only handle HTTP requests
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        # Retrieve token payload set by previous auth middleware
        token_data: Optional[dict] = getattr(request.state, "token", None)
        token_scopes: Set[str] = set()
        if token_data:
            raw = token_data.get("scope") or token_data.get("scopes")
            if raw:
                token_scopes = set(str(raw).split())

        # Determine required scope from the endpoint, if any
        endpoint = scope.get("endpoint")
        required: Optional[str] = getattr(endpoint, "_required_scope", None) if endpoint else None
        if required:
            if required not in token_scopes:
                payload = {"error": "insufficient_scope", "required": required}
                response = JSONResponse(content=payload, status_code=403)
                await response(scope, receive, send)
                return
        # No scope requirement or requirement satisfied – forward the request
        await self.app(scope, receive, send)
