# OPS-14C: Scoped OAuth2 API tokens

## Target paths
- src/alfred_coo/auth/scope_middleware.py
- tests/auth/test_scope_middleware.py

## Acceptance criteria
- Add `src/alfred_coo/auth/scope_middleware.py` exposing a callable middleware (FastAPI/ASGI compatible — match the framework already in use in the repo, inspect `src/alfred_coo/` to confirm).
- Middleware reads scope claims (`scope` or `scopes` claim, space-delimited per RFC 8693) from the JWT validated upstream by the existing token validator in `scoped_tokens.py`.
- Each protected route declares its required scope via a decorator or dependency (e.g. `@requires_scope("fleet:read")`).
- When the validated token's scope set does not contain the required scope, middleware short-circuits and returns HTTP 403 with body exactly `{"error":"insufficient_scope","required":"<scope>"}` (Content-Type `application/json`).
- When the scope claim is entirely absent from the token, treat as empty scope set (deny-by-default).

## Verification
1. `src/alfred_coo/auth/scope_middleware.py` exists.
2. `tests/auth/test_scope_middleware.py` exists with three test cases covering present scope, missing scope, and absent scope.
3. `pytest tests/auth/test_scope_middleware.py` exits with code 0.
4. The missing‑scope test asserts the response body equals `{"error":"insufficient_scope","required":"fleet:read"}`.
