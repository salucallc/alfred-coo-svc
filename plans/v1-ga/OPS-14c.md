# OPS-14c: Scope Middleware

## Target paths
- src/alfred_coo/auth/scope_middleware.py
- tests/auth/test_scope_middleware.py

## Acceptance criteria
**A — Action:**

1. Add `src/alfred_coo/auth/scope_middleware.py` exposing a callable middleware (FastAPI/ASGI compatible — match the framework already in use in the repo, inspect `src/alfred_coo/` to confirm).
2. Middleware reads scope claims (`scope` or `scopes` claim, space-delimited per RFC 8693) from the JWT validated upstream by the existing token validator in `scoped_tokens.py`.
3. Each protected route declares its required scope via a decorator or dependency (e.g. `@requires_scope("fleet:read")`).
4. When the validated token's scope set does not contain the required scope, middleware short-circuits and returns HTTP 403 with body exactly `{"error":"insufficient_scope","required":"<scope>"}` (Content-Type `application/json`).
5. When the scope claim is entirely absent from the token, treat as empty scope set (deny-by-default).

**P — Plan:**

* Inspect `src/alfred_coo/auth/scoped_tokens.py` to understand the validator return shape.
* Pattern the middleware after existing auth middleware in the repo (search for current 401/403 paths).
* Provide a `requires_scope(scope: str)` helper for route declarations.

**E — Evidence:**

* `git diff` showing new middleware file + at least one demonstration route updated.
* `pytest tests/auth/test_scope_middleware.py -v` output, all green.

## Verification approach
Run `pytest tests/auth/test_scope_middleware.py` and ensure exit code 0; manually verify 403 payload matches specification.

## Risks
- Incorrect JWT parsing could allow malformed tokens.
- Adding middleware may affect existing routes; ensure it only adds scopes state.
