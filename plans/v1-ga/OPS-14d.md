# OPS-14D: 24h TTL validation for scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
* Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
* Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
* Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
* Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
* `pytest tests/auth/test_ttl_validation.py -q` exits 0.
* The new `ttl_validator.py` contains a function `validate_token_ttl` that raises a `TokenExpiredError` with status 401 and body `{"error":"token_expired"}` for expired or missing iat.
* `grep -E "86400|24.*3600" src/alfred_coo/auth/ttl_validator.py` returns a match.

## Risks
* No existing code imports `ttl_validator`; wiring may be required later.
* Deleting or modifying existing validator could breach deletion guardrails – we only add a new file.
