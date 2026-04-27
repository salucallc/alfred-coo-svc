from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable


def requires_scope(scope: str) -> Callable:
    """Decorator to declare required OAuth2 scope for a route.

    The decorator attaches a `_required_scope` attribute to the endpoint function.
    """
    def decorator(func: Callable) -> Callable:
        setattr(func, "_required_scope", scope)
        return func
    return decorator


class ScopeMiddleware(BaseHTTPMiddleware):
    """FastAPI/ASGI middleware that enforces required scopes on routes.

    It expects a JWT token to be attached to ``request.state.token`` by a prior
    authentication step. The token should contain either a ``scope`` (string) or
    ``scopes`` (string) claim containing space‑delimited scope identifiers.
    """

    async def dispatch(self, request: Request, call_next):
        # Retrieve token injected by upstream auth middleware.
        token = getattr(request.state, "token", {})
        scope_claim = token.get("scope") or token.get("scopes") or ""
        token_scopes = set(scope_claim.split()) if scope_claim else set()

        # FastAPI stores the endpoint function in ``request.scope['endpoint']``.
        endpoint = request.scope.get("endpoint")
        required = getattr(endpoint, "_required_scope", None) if endpoint else None

        if required is not None:
            if required not in token_scopes:
                return JSONResponse(
                    {"error": "insufficient_scope", "required": required},
                    status_code=403,
                )
        return await call_next(request)

