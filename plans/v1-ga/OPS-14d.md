# OPS-14D: Scoped OAuth2 token TTL validation

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py
- plans/v1-ga/OPS-14d.md

## Acceptance criteria
* Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
* Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
* Tokens missing the `iat` claim are also rejected with the same 401 body (deny‑by‑default).
* Add `tests/auth/test_ttl_validation.py` covering valid, expired, and missing‑iat cases.

## Verification approach
* Unit tests in `tests/auth/test_ttl_validation.py` verify the three scenarios.
* Run `pytest tests/auth/test_ttl_validation.py` and ensure exit code 0.
* Grep the source tree for the TTL constant (`86400` or `24.*3600`).

## Risks
* Introducing a new module may be missed by existing imports; ensure callers import `ttl_validator` where appropriate.
* Changing error responses could affect downstream clients; keep exact JSON shape.
