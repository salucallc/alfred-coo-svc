# OPS-14D: Enforce 24‑hour TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
**A — Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny‑by‑default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing‑iat.

## Verification approach
- Added `ttl_validator.py` implementing `validate_iat` with 86400‑second cutoff.
- Imported validator in `scoped_tokens.py` for future integration.
- Created `tests/auth/test_ttl_validation.py` with three pytest cases using freezegun.
- Running `pytest -q tests/auth/test_ttl_validation.py` exits with code 0.
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` returns a match for the TTL constant.

## Risks
- No existing request‑handling code invokes `validate_iat`; integration work may be needed later.
- Deleting or heavily modifying `scoped_tokens.py` could impact other OPS‑14 scaffolding; only an import was added.
