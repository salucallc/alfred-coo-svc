# OPS-14d-d: End-to-end TTL test (token issued -> 24h+ later -> rejected)

**Linear:** SAL-3275

**Parent:** OPS-14d (SAL-3038) -- see `plans/v1-ga/OPS-14d.md`
**Wave:** 3
**Auto-dispatchable:** yes (no human-assigned label)

## Context

End-to-end smoke test exercising the full OPS-14d path: issue a token with `iat = now`, advance time past the 24h TTL boundary using `freezegun` (or equivalent monkeypatched clock), present the token through the FastAPI test client, and assert the request is rejected with 401 + `{"error":"token_expired"}` body.

Depends on OPS-14d-a (constant + shape), OPS-14d-b (validator), and OPS-14d-c (middleware wiring) all being merged.

## Target paths

- `tests/auth/test_e2e_token_ttl.py` (new)

## APE/V Acceptance (machine-checkable)

1. File `tests/auth/test_e2e_token_ttl.py` exists with the test `test_token_rejected_after_24h_boundary`.
2. The test issues a token with `iat = now`, advances the clock to `now + 86401`, presents the token through the test client / middleware entrypoint, and asserts response status 401 with JSON body `{"error":"token_expired"}`.
3. `pytest tests/auth/test_e2e_token_ttl.py -v` exits 0.

## Out of scope

- Production OAuth provider integration -- parent SAL-2647.
- Portal token-rotation UI -- parent SAL-2647.
