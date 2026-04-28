# OPS-14D: Enforce 24h TTL on scoped tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Extend the token validator to reject tokens where `now_unix - iat > 86400` seconds.
- Rejected requests must return HTTP 401 with body exactly `{"error":"token_expired"}`.
- Tokens missing the `iat` claim must also be rejected with the same 401 response.
- Add tests covering valid, expired, and missing-iat cases.

## Verification approach
- Run `pytest tests/auth/test_ttl_validation.py` and ensure exit code 0.
- Grep the source for `86400` or `24.*3600` to confirm TTL constant presence.

## Risks
- Incorrect handling of non-integer iat values.
- Potential performance impact if validator called excessively.
- Need to ensure error format matches exactly, no extra fields.
