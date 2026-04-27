'''Scope validation middleware and helper for FastAPI routes.'''

from fastapi import Request, Depends
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class ScopeMiddleware(BaseHTTPMiddleware):
    """Pass‑through middleware that ensures ``request.state.scopes`` exists.

    The real scope checks are performed by the ``requires_scope`` dependency.
    """

    async def dispatch(self, request: Request, call_next):
        # Existing authentication should populate ``request.state.scopes``.
        # If it is missing we treat it as an empty set (deny‑by‑default).
        if not hasattr(request.state, "scopes"):
            request.state.scopes = set()
        response = await call_next(request)
        return response


def _require_scope(required_scope: str):
    """Dependency that enforces *required_scope* is present.

    Returns a ``True`` value when the scope is allowed, otherwise a ``JSONResponse``
    with HTTP 403 and the exact payload required by the APE/V.
    """

    async def dependency(request: Request):
        scopes = getattr(request.state, "scopes", set())
        if required_scope not in scopes:
            return JSONResponse(
                status_code=403,
                content={"error": "insufficient_scope", "required": required_scope},
            )
        return True

    return Depends(dependency)


def requires_scope(scope: str):
    """FastAPI‑compatible helper used as a dependency on route handlers.

    Example::

        @app.get("/fleet")
        async def fleet_endpoint(allowed=requires_scope("fleet:read")):
            return {"ok": True}
    """

    return _require_scope(scope)
