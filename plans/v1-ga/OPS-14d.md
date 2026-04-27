# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
- Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
- Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
- Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
- `tests/auth/test_ttl_validation.py` exists with three named tests.
- Running `pytest tests/auth/test_ttl_validation.py` exits 0.
- Tests assert response body equals `{"error":"token_expired"}` for expired and missing-iat cases.
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` returns at least one match.

## Risks
- Deleting existing validator logic could exceed deletion guardrails; we add new sibling file instead of modifying existing code.
- Tests rely on freezegun; ensure dependency is present in CI.
