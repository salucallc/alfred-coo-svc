# OPS-14d: Enforce 24h TTL on scoped OAuth2 tokens

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

**P — Plan:**
* Read existing validator to find the integration point.
* Use `time.time()` for `now_unix`; freeze with `freezegun` or stub in tests.
* Ensure the 401 body matches exactly — no extra fields.

**E — Evidence:**
* `git diff` of validator change + new test file.
* `pytest tests/auth/test_ttl_validation.py -v` output, all three cases green.

## Verification approach
- Run `pytest tests/auth/test_ttl_validation.py`; all tests must pass (exit code 0).
- Verify source contains the constant `86400` or regex `24.*3600`.

## Risks
- Deleting existing code beyond TTL logic may breach deletion guardrail.
- Incorrect response format could cause downstream failures.
