# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
- Extend token validator to reject tokens older than 86400 seconds or missing `iat` claim, returning HTTP 401 with body `{"error":"token_expired"}`.
- Add tests verifying valid, expired, and missing `iat` cases.
- Ensure TTL constant appears in source (grep matches).

## Verification approach
- Run `pytest tests/auth/test_ttl_validation.py` expecting exit code 0.
- Manual curl test can verify 401 response body.

## Risks
- Deleting more than allowed lines from existing validator (none, only addition).
- Potential mismatch of error body formatting.
- Ensure import of FastAPI HTTPException works in test environment.
