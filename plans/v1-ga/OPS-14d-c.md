# OPS-14d-c: Wire TTL validator into scoped-token auth middleware

**Linear:** SAL-3274

**Parent:** OPS-14d (SAL-3038) -- see `plans/v1-ga/OPS-14d.md`
**Wave:** 3
**Auto-dispatchable:** yes (no human-assigned label)

## Context

Calls the OPS-14d-b validator from the scoped-token middleware so every request bearing a scoped token triggers a TTL check before the request is allowed through. On rejection the middleware must return HTTP 401 with body exactly `{"error":"token_expired"}` and `Content-Type: application/json`.

The integration point lives in `src/alfred_coo/auth/scoped_tokens.py` (the existing module that holds `get_token` and is already importing from `ttl_validator`). The wiring may also touch a sibling middleware module if one exists; pick the lowest-cost integration point that is exercised on every request.

Child of SAL-3038 -- depends on OPS-14d-a + OPS-14d-b.

## Target paths

- `src/alfred_coo/auth/scoped_tokens.py` (call validator on token presentation)
- `tests/auth/test_scoped_tokens_ttl_integration.py` (new, 1 integration test)

## APE/V Acceptance (machine-checkable)

1. File `tests/auth/test_scoped_tokens_ttl_integration.py` exists with at minimum the test `test_expired_token_rejected_with_401_body`.
2. The integration test asserts the rejected request returns status 401 and JSON body equal to `{"error":"token_expired"}`.
3. `pytest tests/auth/test_scoped_tokens_ttl_integration.py -v` exits 0.
4. `grep -E "enforce_ttl|TOKEN_TTL_SECONDS" src/alfred_coo/auth/scoped_tokens.py` returns at least one match (validator is invoked, not just imported).

## Out of scope

- Live HTTP round-trip from a real client -- OPS-14d-d.
