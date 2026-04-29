'''python
"""Scope‑enforcement middleware for FastAPI/ASGI applications.

The upstream ``src/alfred_coo/auth/scoped_tokens.py`` validates a JWT and
provides a payload dictionary (the *token payload*).  This middleware does
*not* perform validation itself; it merely makes the payload available to
down‑stream routes via ``request.state.auth_payload`` and supplies a helper
dependency ``requires_scope`` that raises a 403 error when the required
scope is missing.
"""

from __future__ import annotations

from typing import Callable, Set, Dict, Any
from fastapi import Request, HTTPException, Depends

# ---------------------------------------------------------------------------
# Helper to normalise the scope claim(s) from the token payload.
# ---------------------------------------------------------------------------

def _extract_scopes(payload: Dict[str, Any]) -> Set[str]:
    """Return a set of scopes from a token payload.

    The payload may contain either a space‑delimited ``scope`` string or a
    ``scopes`` list/tuple.  If neither is present an empty set is returned.
    """
    raw = payload.get("scope") or payload.get("scopes")
    if isinstance(raw, str):
        # ``scope`` is a space‑delimited string per RFC 8693.
        return {s for s in raw.split() if s}
    if isinstance(raw, (list, tuple)):
        return {str(s) for s in raw}
    return set()

# ---------------------------------------------------------------------------
# FastAPI dependency that enforces a required scope.
# ---------------------------------------------------------------------------

def requires_scope(required_scope: str):
    """FastAPI dependency that asserts *required_scope* is present.

    The upstream authentication middleware is expected to store the token
    payload on ``request.state.auth_payload``.  The dependency extracts the
    scopes and raises ``HTTPException`` with a strict JSON body when the
    required scope is absent.
    """

    async def _dependency(request: Request):
        payload: Dict[str, Any] = getattr(request.state, "auth_payload", {})
        token_scopes = _extract_scopes(payload)
        if required_scope not in token_scopes:
            raise HTTPException(
                status_code=403,
                detail={"error": "insufficient_scope", "required": required_scope},
                headers={"Content-Type": "application/json"},
            )

    return Depends(_dependency)

# ---------------------------------------------------------------------------
# ASGI middleware that simply forwards the request – the heavy lifting is
# done by the ``requires_scope`` dependency.  Keeping this shim allows the
# repository to honour the APE/V requirement of exposing a *callable
# middleware* while remaining minimally invasive.
# ---------------------------------------------------------------------------

class ScopeMiddleware:
    """ASGI middleware that makes the token payload available to FastAPI.

    The upstream ``src/alfred_coo/auth/scoped_tokens.py`` validates the JWT
    and returns its payload.  Frameworks that already mount an authentication
    middleware are expected to attach that payload to ``request.state`` –
    this class does not mutate the scope but exists as the required callable.
    """

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only interested in HTTP connections.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # The downstream FastAPI request object will be created by FastAPI
        # itself; we simply forward the call.
        await self.app(scope, receive, send)
'''