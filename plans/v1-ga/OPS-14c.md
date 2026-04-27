# OPS-14C: Scoped OAuth2 API tokens

## Target paths
- src/alfred_coo/auth/scope_middleware.py
- tests/auth/test_scope_middleware.py

## Acceptance criteria
- Middleware validates `scope`/`scopes` claim from JWT set on `request.state.token`.
- Missing required scope returns 403 with JSON body `{"error":"insufficient_scope","required":"<scope>"}`.
- No scope claim treated as empty set, denying access.
- Provided `requires_scope` decorator for route declarations.
- Tests cover present, missing, and absent scope scenarios and all pass.

## Verification approach
- `src/alfred_coo/auth/scope_middleware.py` exists.
- `tests/auth/test_scope_middleware.py` exists with three test cases.
- Running `pytest tests/auth/test_scope_middleware.py` exits 0.

## Risks
- Relies on upstream auth middleware populating `request.state.token`.
- If token payload key differs, middleware will deny all requests.
