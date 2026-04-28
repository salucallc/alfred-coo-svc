# OPS-14D: Enforce 24 hour TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
- Token validator rejects requests where `now_unix - iat > 86400` with HTTP 401 and body `{"error":"token_expired"}`.
- Tokens missing `iat` are also rejected with the same response.
- Added tests covering valid, expired, and missing `iat` cases, all passing.

## Verification approach
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` confirms TTL constant present.
- `pytest tests/auth/test_ttl_validation.py` exits 0 with three green tests.

## Risks
- No existing integration point; additional wiring may be needed in request handling pipeline.
- Deleting or heavily modifying `scoped_tokens.py` could break other auth flows; we added a sibling module to minimise impact.
