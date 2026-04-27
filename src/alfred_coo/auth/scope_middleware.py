import json
from typing import Callable, List, Optional
from fastapi import Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from .scoped_tokens import get_token  # placeholder import; actual token validation occurs upstream


def _parse_scopes(token_payload: dict) -> List[str]:
    """Extract the scope claim(s) from a token payload.
    Supports the `scope` (space‑delimited string) or `scopes` (list) claim.
    """
    scopes_raw = token_payload.get("scope") or token_payload.get("scopes")
    if not scopes_raw:
        return []
    if isinstance(scopes_raw, str):
        return scopes_raw.split()
    if isinstance(scopes_raw, list):
        # assume already a list of strings
        return [str(s) for s in scopes_raw]
    # Unexpected type – treat as empty
    return []


def requires_scope(required: str) -> Callable[[Callable], Callable]:
    """Dependency/decorator that records the required scope on the endpoint.
    Used by the middleware to check against the token's scope set.
    """
    def decorator(func: Callable) -> Callable:
        setattr(func, "_required_scope", required)
        return func
    return decorator


class ScopeMiddleware:
    """FastAPI middleware that enforces required OAuth2 scopes.

    It expects that an upstream authentication step has attached the decoded JWT
    payload to ``request.state.token_payload`` (a ``dict``). The middleware then
    extracts the scope claim(s) and, for any route that has a ``_required_scope``
    attribute (set via ``requires_scope``), verifies inclusion. If missing, a
    403 JSON response is returned.
    """

    def __init__(self, app):
        self.app = app
        # FastAPI will call ``self.__call__`` for each request

    async def __call__(self, scope, receive, send):
        # FastAPI passes ASGI scope dict; we need the request object
        request = Request(scope, receive=receive)
        # Retrieve token payload injected by previous auth step, if any
        token_payload: Optional[dict] = getattr(request.state, "token_payload", None)
        token_scopes = _parse_scopes(token_payload or {})

        # Resolve the route handler to inspect required scope
        route: APIRoute = request.scope.get("route")  # type: ignore
        endpoint = getattr(route, "endpoint", None)
        required_scope = getattr(endpoint, "_required_scope", None) if endpoint else None

        if required_scope and required_scope not in token_scopes:
            # Scope missing – deny request
            body = {"error": "insufficient_scope", "required": required_scope}
            response = JSONResponse(content=body, status_code=403)
            await response(scope, receive, send)
            return
        # Scope satisfied or not required – continue down the stack
        await self.app(scope, receive, send)

# Helper to add the middleware to a FastAPI app instance
def add_scope_middleware(app):
    """Convenience function to mount ``ScopeMiddleware`` on a FastAPI app.
    ``app`` is expected to be a ``FastAPI`` instance.
    """
    app.add_middleware(ScopeMiddleware)
