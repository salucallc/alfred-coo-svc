# OPS-14D: Enforce 24h TTL on scoped OAuth2 tokens

## Target paths
- src/alfred_coo/auth/scoped_tokens.py
- src/alfred_coo/auth/ttl_validator.py
- tests/auth/test_ttl_validation.py

## Acceptance criteria
**A — Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny‑by‑default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now‑1h), expired (iat = now‑25h), missing‑iat.

**P — Plan:**

* Read existing validator to find the integration point.
* Use `time.time()` for `now_unix`; freeze with `freezegun` or stub in tests.
* Ensure the 401 body matches exactly — no extra fields.

**E — Evidence:**

* `git diff` of validator change + new test file.
* `pytest tests/auth/test_ttl_validation.py -v` output, all three cases green.

**V — Verification (machine‑checkable):**

1. File `tests/auth/test_ttl_validation.py` exists with three named tests:
   * `test_valid_recent_iat_passes`
   * `test_expired_iat_returns_401`
   * `test_missing_iat_returns_401`
2. `pytest tests/auth/test_ttl_validation.py` exits 0.
3. Tests assert response body equals `{"error":"token_expired"}` for expired and missing‑iat cases.
4. `grep -E "86400|24.*3600" src/alfred_coo/auth/` returns at least one match (the TTL constant).

## Verification approach
Added tests using `freezegun` to lock time and assert proper status codes. Running `pytest` confirms all pass.

## Risks
Potential regression if other token handling paths bypass `ttl_validator`. Ensure any future validator extensions import and use `validate_iat`.
