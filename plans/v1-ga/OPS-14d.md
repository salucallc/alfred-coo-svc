# OPS-14D: Enforce 24h TTL on scoped tokens

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
- Added `src/alfred_coo/auth/ttl_validator.py` with TTL check logic.
- Implemented `tests/auth/test_ttl_validation.py` using `freezegun` to control current time.
- All three tests pass (`pytest -q` returns exit code 0).
- Verified presence of TTL constant with `grep -E "86400|24.*3600" src/alfred_coo/auth/`.
