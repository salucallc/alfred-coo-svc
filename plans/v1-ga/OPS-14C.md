# OPS-14C: Add scope middleware and tests

## Target paths
- src/alfred_coo/auth/scope_middleware.py
- tests/auth/test_scope_middleware.py

## Acceptance criteria
* File `src/alfred_coo/auth/scope_middleware.py` exists.
* File `tests/auth/test_scope_middleware.py` exists with at least three test cases:
  * `test_scope_present_returns_200`
  * `test_scope_missing_returns_403_with_payload`
  * `test_no_scope_claim_returns_403`
* `pytest tests/auth/test_scope_middleware.py` exits 0.
* Test asserts response body equals `{"error":"insufficient_scope","required":"<scope>"}` for the missing‑scope case.

## Verification approach
Added FastAPI app with a token injection middleware and the new scope middleware. Unit tests exercise required‑scope enforcement for present, missing, and absent scope claims.

## Risks
* Relies on upstream authentication populating ``request.state.token``. If that contract changes, middleware will deny all requests.
* Deleting or modifying existing auth middleware could affect behavior; this file is additive only.
