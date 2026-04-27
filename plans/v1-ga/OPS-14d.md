# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
* token issued > 24h ago → 401 with token_expired error code
* token missing iat → 401 with token_expired error code
* valid token passes

## Verification approach
Added unit tests in tests/auth/test_ttl_validation.py covering valid, expired, missing iat. Run `pytest -q`. Verified grep finds TTL constant.

## Risks
* None identified; limited to token validation path. Potential performance impact negligible.
