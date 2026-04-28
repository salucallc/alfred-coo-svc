# OPS-14C: Scoped OAuth2 API tokens - scope middleware

## Target paths
- `src/alfred_coo/auth/scope_middleware.py` (new)
- `tests/auth/test_scope_middleware.py` (new)
- `plans/v1-ga/OPS-14c.md` (new)

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

**V — Verification (machine-checkable):**

1. File `src/alfred_coo/auth/scope_middleware.py` exists.
2. File `tests/auth/test_scope_middleware.py` exists with at least three test cases:
   * `test_scope_present_returns_200` (or equivalent)
   * `test_scope_missing_returns_403_with_payload`
   * `test_no_scope_claim_returns_403`
3. `pytest tests/auth/test_scope_middleware.py` exits 0.
4. Test asserts response body equals `{"error":"insufficient_scope","required":"<scope>"}` for the missing-scope case.

## Verification approach

* Middleware implemented as FastAPI BaseHTTPMiddleware for ASGI compatibility
* RFC 8693 space-delimited scope parsing from request.state.scopes
* `@requires_scope()` dependency for route-level scope declarations  
* Exact 403 payload validation in test suite
* Deny-by-default behavior when scope claim absent

## Risks

* **Upstream token validator contract**: Assumes token validator populates `request.state.scopes` - documented in code comments
* **Middleware ordering**: ScopeMiddleware must be added after authentication middleware but before route handlers
* **Scope format compatibility**: RFC 8693 space-delimited strings supported; list format also accepted for flexibility