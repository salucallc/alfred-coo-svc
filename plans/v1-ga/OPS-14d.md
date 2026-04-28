# OPS-14D: TTL validation for scoped tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
- Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
- Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
- Tokens missing the `iat` claim are also rejected with the same 401 body (deny‑by‑default).
- Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now‑1h), expired (iat = now‑25h), missing‑iat.

## Verification approach
- New module `ttl_validator.py` implements `validate_token_iat(token: dict) -> None` raising `HTTPException` with status 401 and body `{"error":"token_expired"}` when the token is expired or missing `iat`.
- Tests in `tests/auth/test_ttl_validation.py` use `freezegun` to control time and assert correct HTTP 401 responses and body content.
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` confirms the TTL constant is present.
- Running `pytest tests/auth/test_ttl_validation.py` exits with status 0.