# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
- Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
- Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
- Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
- Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

## Verification approach
- Run `pytest tests/auth/test_ttl_validation.py` expecting exit code 0.
- Grep repository for `86400` or regex `24.*3600` in `src/alfred_coo/auth/` to confirm TTL constant.

## Risks
- No modifications to existing `scoped_tokens.py` reduces risk of breaking current auth flow.
- Introducing new module requires import path updates if future code integrates it.
