# Added APE/V citation for SAL-3037
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import base64
import json
from typing import Callable, List

class ScopeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization")
        scopes: List[str] = []
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1]
            parts = token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                try:
                    payload_bytes = base64.urlsafe_b64decode(payload_b64)
                    payload = json.loads(payload_bytes)
                    claim = payload.get("scope") or payload.get("scopes")
                    if isinstance(claim, str):
                        scopes = claim.split()
                    elif isinstance(claim, list):
                        scopes = claim
                except Exception:
                    pass
        request.state.scopes = scopes
        response = await call_next(request)
        return response

def requires_scope(required: str) -> Callable[[Request], None]:
    async def dependency(request: Request):
        if required not in getattr(request.state, "scopes", []):
            raise HTTPException(
                status_code=403,
                content={"error": "insufficient_scope", "required": required},
            )
    return dependency