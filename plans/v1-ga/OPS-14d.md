# OPS-14D: enforce 24h TTL on scoped tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
**A — Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
- Added `ttl_validator.py` implementing the TTL check.
- Updated `scoped_tokens.py` with helper import and `validate_iat` wrapper.
- Created `tests/auth/test_ttl_validation.py` using freezegun to assert correct behavior.
- All tests pass (`pytest -q` returns exit code 0).
- grep for `86400` or `24.*3600` in `src/alfred_coo/auth/` returns the constant in `ttl_validator.py`.
