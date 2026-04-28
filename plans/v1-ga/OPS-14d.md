# OPS-14D: TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Tokens older than 24h are rejected with HTTP 401 and body `{"error":"token_expired"}`.
- Tokens missing the `iat` claim are also rejected with the same 401 body.
- New tests `test_valid_recent_iat_passes`, `test_expired_iat_returns_401`, `test_missing_iat_returns_401` all pass.
- A grep for `86400` or `24.*3600` finds the TTL constant in the source.

## Verification approach
- Run `pytest tests/auth/test_ttl_validation.py` – must exit 0.
- Manual test of the validator (if integrated) confirms rejection behavior.
- Code review ensures the constant and check function are present.

## Risks
- Deleting or altering existing token flow may affect other services; changes limited to TTL check.
- Exception handling uses `ValueError`, which downstream may need to map to HTTP 401.
- Future token formats may change `iat` handling – adjust validator accordingly.
