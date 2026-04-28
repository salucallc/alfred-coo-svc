# OPS-14D: Scoped OAuth2 token TTL enforcement

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Extend the token validator to reject tokens older than 24 hours.
- Return HTTP 401 with body exactly `{"error":"token_expired"}` for expired or missing `iat` claims.
- Add a test suite covering valid, expired, and missing‑iat scenarios.

## Verification approach
- Added `ttl_validator.py` with `validate_iat` helper.
- New pytest file `tests/auth/test_ttl_validation.py` uses freezegun to freeze time and asserts correct behavior.
- CI runs `pytest` and confirms exit code 0.
- Grep for the TTL constant confirms presence in the source.

## Risks
- Deleting or modifying existing validation logic could exceed allowed deletions; we added a sibling module instead.
- Ensure the new validator is integrated where tokens are checked; currently scoped token flow calls `validate_iat` before proceeding.
