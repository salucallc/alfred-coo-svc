# OPS-14c: FastAPI scope middleware

## Target paths
- src/alfred_coo/auth/scope_middleware.py
- tests/auth/test_scope_middleware.py
- plans/v1-ga/OPS-14c.md

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
Added unit tests exercising present, missing, and absent scope scenarios; all pass. Manual smoke test using FastAPI TestClient confirms 403 payload format.

## Risks
- Potential mismatch with upstream token payload location (requires ``request.state.token_payload``). Ensure compatibility with existing auth flow.
- Adding middleware may affect existing routes if they lack required scope annotation; default deny‑by‑default is intentional.
