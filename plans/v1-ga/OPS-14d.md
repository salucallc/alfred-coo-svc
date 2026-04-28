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

## Plan doc
Enforces TTL via `ttl_validator.py` and integrates a thin wrapper in `scoped_tokens.py` for future use.

## Verification approach
- Unit tests in `tests/auth/test_ttl_validation.py` validate all cases.
- `grep -E "86400|24.*3600" src/alfred_coo/auth/` confirms constant presence.
- CI runs `pytest` ensuring zero failures.

## Risks
- Potential performance impact of time call on each token validation (negligible).
- Misconfiguration of system clock could affect TTL checks.
