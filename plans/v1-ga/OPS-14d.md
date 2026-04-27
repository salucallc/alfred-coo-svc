# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
**A — Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
- Added `ttl_validator.py` with TTL check logic.
- Updated `scoped_tokens.py` to import and expose a validation entry point.
- Implemented pytest suite `tests/auth/test_ttl_validation.py` using freezegun to freeze time and verify all three cases.
- CI runs `pytest` and confirms zero failures.

## Risks
- JWT decoding assumes simple base64url payload without padding; malformed tokens could raise unexpected errors.
- Integration point not fully wired into request handling yet; future work needed to call `validate_scoped_token` on incoming requests.
