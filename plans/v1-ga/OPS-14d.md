# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
**A — Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
- Added `ttl_validator.py` with TTL check and `TokenExpiredError` matching required response.
- Implemented pytest suite `tests/auth/test_ttl_validation.py` verifying valid, expired, and missing‑iat cases.
- All tests pass (`pytest -q` returns exit code 0).
- `grep -E "86400|24.*3600" src/alfred_coo/auth/ttl_validator.py` confirms presence of the TTL constant.

## Risks
- Introduces a new module; existing code must import and use `validate_token` (out of scope for this ticket).
- Hard‑coded TTL constant may need future adjustment.
- Exception handling assumes callers interpret `TokenExpiredError` correctly.
