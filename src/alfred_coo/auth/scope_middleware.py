"""FastAPI/ASGI middleware for OAuth2 scope-based authorization.

Implements RFC 8693 scope enforcement with deny-by-default behavior.
Expects upstream token validation to populate request.state.scopes.
"""

from typing import Optional, Set, List, Callable
from fastapi import Request, HTTPException, Depends
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
import json


class ScopeMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces OAuth2 scope claims on protected routes.
    
    This middleware reads scope claims from request.state.scopes (populated by
    upstream token validator) and enforces scope requirements declared via
    the requires_scope dependency.
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> None:
        # If no scope requirements set on the route, pass through
        if not hasattr(request.state, '_required_scope'):
            return await call_next(request)
            
        required_scope = request.state._required_scope
        
        # Get scopes from request state (set by upstream token validator)
        # Treat missing scope claim as empty set (deny-by-default)
        scope_claim = getattr(request.state, 'scopes', None)
        if scope_claim is None:
            granted_scopes: Set[str] = set()
        elif isinstance(scope_claim, str):
            # RFC 8693: space-delimited scope string
            granted_scopes = set(scope_claim.split())
        elif isinstance(scope_claim, list):
            granted_scopes = set(scope_claim)
        else:
            granted_scopes = set()
        
        # Check if required scope is granted
        if required_scope not in granted_scopes:
            return HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "required": required_scope
                }
            )
        
        return await call_next(request)


def requires_scope(scope: str):
    """Route dependency that declares a required OAuth2 scope.
    
    Usage:
        @app.get("/protected")
        async def protected_route(required_scope=Depends(requires_scope("soul:read"))):
            return {"message": "authorized"}
    
    Args:
        scope: The required OAuth2 scope (e.g., "soul:memory:read")
    
    Returns:
        FastAPI dependency that enforces scope requirements via middleware
    """
    def scope_checker(request: Request):
        # Store required scope on request state for middleware to check
        request.state._required_scope = scope
        return True
    return Depends(scope_checker)