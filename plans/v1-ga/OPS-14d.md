# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Tokens older than 24h are rejected with HTTP 401 and body `{"error":"token_expired"}`.
- Tokens missing the `iat` claim are also rejected with the same 401 response.
- Valid tokens (issued within 24h) pass without exception.

## Verification approach
- Added `ttl_validator.py` implementing `validate_iat` with 24h TTL check.
- Updated `scoped_tokens.py` to import the validator (no functional change to existing stub).
- Created `tests/auth/test_ttl_validation.py` covering valid, expired, and missing‑iat scenarios using pytest.
- CI runs `pytest` ensuring all tests pass (exit code 0).
- Grep for `86400` or `24.*3600` in `src/alfred_coo/auth/` confirms TTL constant presence.
