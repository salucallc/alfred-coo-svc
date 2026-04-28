# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Tokens older than 24h are rejected with HTTP 401 and body `{"error":"token_expired"}`.
- Tokens missing `iat` claim are rejected with same response.
- Valid tokens within TTL pass.

## Verification approach
- Unit tests in `tests/auth/test_ttl_validation.py` covering valid, expired, and missing `iat` cases.
- Ensure TTL constant `86400` appears in source.

## Risks
- None identified; low impact change limited to validation logic.
