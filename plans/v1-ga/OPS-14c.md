# OPS-14c: scope-enforcement middleware for OAuth2 tokens

**Parent:** SAL-2647 (OPS-14 Scoped OAuth2 API tokens) — see `plans/v1-ga/OPS-14.md`
**Linear:** SAL-3037
**Wave:** 3

## Context

PR #169 (`salucallc/alfred-coo-svc`) shipped `src/alfred_coo/auth/scoped_tokens.py` and `tests/test_scoped_oauth_tokens.py` covering token issuance + scope claim shape. What remains for the autonomous pipeline: a middleware that enforces required scopes per-route at request time. Portal rotation UI and integration tests against a real OAuth provider stay human-only on the parent.

## APE/V Acceptance

**A â€” Action:**

1. Add `src/alfred_coo/auth/scope_middleware.py` exposing a callable middleware (FastAPI/ASGI compatible â€” match the framework already in use in the repo, inspect `src/alfred_coo/` to confirm).
2. Middleware reads scope claims (`scope` or `scopes` claim, space-delimited per RFC 8693) from the JWT validated upstream by the existing token validator in `scoped_tokens.py`.
3. Each protected route declares its required scope via a decorator or dependency (e.g. `@requires_scope("fleet:read")`).
4. When the validated token's scope set does not contain the required scope, middleware short-circuits and returns HTTP 403 with body exactly `{"error":"insufficient_scope","required":"<scope>"}` (Content-Type `application/json`).
5. When the scope claim is entirely absent from the token, treat as empty scope set (deny-by-default).

**P â€” Plan:**

* Inspect `src/alfred_coo/auth/scoped_tokens.py` to understand the validator return shape.
* Pattern the middleware after existing auth middleware in the repo (search for current 401/403 paths).
* Provide a `requires_scope(scope: str)` helper for route declarations.

**E â€” Evidence:**

* `git diff` showing new middleware file + at least one demonstration route updated.
* `pytest tests/auth/test_scope_middleware.py -v` output, all green.

**V â€” Verification (machine-checkable):**

1. File `src/alfred_coo/auth/scope_middleware.py` exists.
2. File `tests/auth/test_scope_middleware.py` exists with at least three test cases:
   * `test_scope_present_returns_200` (or equivalent)
   * `test_scope_missing_returns_403_with_payload`
   * `test_no_scope_claim_returns_403`
3. `pytest tests/auth/test_scope_middleware.py` exits 0.
4. Test asserts response body equals `{"error":"insufficient_scope","required":"<scope>"}` for the missing-scope case.
