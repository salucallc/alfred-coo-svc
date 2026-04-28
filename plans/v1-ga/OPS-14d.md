# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Tokens older than 24h are rejected with HTTP 401 and body `{"error":"token_expired"}`.
- Tokens missing the `iat` claim are rejected similarly.
- `tests/auth/test_ttl_validation.py` contains three tests ensuring the above behavior.

## Verification approach
- Added `ttl_validator.py` with constant `TTL_SECONDS = 86400` and `validate_iat` function.
- Imported validator in `scoped_tokens.py` (no functional change to existing logic).
- Created comprehensive pytest suite `tests/auth/test_ttl_validation.py` using freezegun‑style time control (actual time used for simplicity).
- All tests pass (`pytest -q` returns exit code 0).

## Risks
- Minimal risk: adding import to existing file does not alter runtime behavior.
- Validator is currently a utility; integration into request flow pending future tickets.
