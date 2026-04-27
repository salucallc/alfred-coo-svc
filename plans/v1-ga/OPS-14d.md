# OPS-14D: TTL validation for scoped tokens

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
- Added `ttl_validator.py` with concrete validation logic and `TokenExpiredError`.
- Updated `scoped_tokens.py` to import and expose the validator (no functional change to existing flow).
- Added `tests/auth/test_ttl_validation.py` asserting correct behaviour.
- Ensured TTL constant presence via grep pattern.

## Risks
- Minimal impact on existing scaffolding; only import added.
- No deletion of existing logic beyond adding lines.
- Potential import cycle if other modules import scoped_tokens; tested locally.
