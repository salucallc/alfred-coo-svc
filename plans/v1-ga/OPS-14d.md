# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Tokens older than 24h are rejected with HTTP 401 and body {"error":"token_expired"}.
- Tokens missing iat claim are rejected similarly.
- Valid token (issued within 24h) passes.

## Verification approach
- Unit tests in tests/auth/test_ttl_validation.py cover valid, expired, missing cases.
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` finds TTL constant usage.

## Risks
- No backward‑compatibility risks; only adds validation logic without removing existing code.
