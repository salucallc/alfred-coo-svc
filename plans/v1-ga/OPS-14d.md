# OPS-14d: 24h TTL validation for issued tokens

**Parent:** SAL-2647 (OPS-14 Scoped OAuth2 API tokens) — see `plans/v1-ga/OPS-14.md`
**Linear:** SAL-3038
**Wave:** 3

## Context

PR #169 introduced `src/alfred_coo/auth/scoped_tokens.py`. Tokens currently lack a hard 24h time-to-live check independent of the OAuth provider's exp claim handling. Per the v1-GA hardening pass, every token must be rejected if its `iat` (issued-at) is older than 24h, regardless of `exp`. This protects against unexpired-but-stale tokens after a key compromise.

## APE/V Acceptance

**A â€” Action:**

1. Extend the token validator in `src/alfred_coo/auth/scoped_tokens.py` (or a sibling `ttl_validator.py` if cleaner) so that on every request the validator computes `now_unix - iat` and rejects when the delta exceeds 86400 seconds.
2. Rejected requests return HTTP 401 with body exactly `{"error":"token_expired"}` (Content-Type `application/json`).
3. Tokens missing the `iat` claim are also rejected with the same 401 body (deny-by-default).
4. Add `tests/auth/test_ttl_validation.py` covering: valid (iat = now-1h), expired (iat = now-25h), missing-iat.

**P â€” Plan:**

* Read existing validator to find the integration point.
* Use `time.time()` for `now_unix`; freeze with `freezegun` or stub in tests.
* Ensure the 401 body matches exactly â€” no extra fields.

**E â€” Evidence:**

* `git diff` of validator change + new test file.
* `pytest tests/auth/test_ttl_validation.py -v` output, all three cases green.

**V â€” Verification (machine-checkable):**

1. File `tests/auth/test_ttl_validation.py` exists with three named tests:
   * `test_valid_recent_iat_passes`
   * `test_expired_iat_returns_401`
   * `test_missing_iat_returns_401`
2. `pytest tests/auth/test_ttl_validation.py` exits 0.
3. Tests assert response body equals `{"error":"token_expired"}` for expired and missing-iat cases.
4. `grep -E "86400|24.*3600" src/alfred_coo/auth/` returns at least one match (the TTL constant).
