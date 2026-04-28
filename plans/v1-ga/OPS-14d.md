# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- token issued > 24h ago → 401 with `token_expired` error code
- token missing `iat` → 401 with `token_expired`
- valid token passes (no exception raised)

## Verification approach
- pytest `tests/auth/test_ttl_validation.py` passes (exit code 0)
- `grep -E "86400|24.*3600" src/alfred_coo/auth/ttl_validator.py` returns at least one match

## Risks
- No deletions of existing code, only additive import
- Ensure new file does not interfere with existing imports
