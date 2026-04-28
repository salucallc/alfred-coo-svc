# OPS-14d-b: TTL validator function with boundary-case unit tests

**Linear:** SAL-3273

**Parent:** OPS-14d (SAL-3038) -- see `plans/v1-ga/OPS-14d.md`
**Wave:** 3
**Auto-dispatchable:** yes (no human-assigned label)

## Context

Implements the pure-function TTL validator on top of OPS-14d-a's constant + claim shape. Given an `iat` claim, the function rejects when `now_unix - iat > 86400` and when `iat` is missing (deny-by-default). Rejection is signalled either by raising or returning an explicit failure value; the rejection MUST carry the body shape `{"error":"token_expired"}` so the middleware (OPS-14d-c) can return a verbatim 401.

This child also lands the unit-test file `tests/auth/test_ttl_validation.py` with three named tests covering valid, expired, and missing-iat boundaries.

Child of SAL-3038 -- depends on OPS-14d-a (constant + shape).

## Target paths

- `src/alfred_coo/auth/ttl_validator.py` (extend with validator function)
- `tests/auth/test_ttl_validation.py` (new)
- `tests/auth/__init__.py` (new if missing)

## APE/V Acceptance (machine-checkable)

1. File `tests/auth/test_ttl_validation.py` exists with three tests named exactly:
   - `test_valid_recent_iat_passes`
   - `test_expired_iat_returns_401`
   - `test_missing_iat_returns_401`
2. The expired and missing-iat tests assert the rejection carries body equal to `{"error":"token_expired"}`.
3. `pytest tests/auth/test_ttl_validation.py -v` exits 0.

## Out of scope

- Middleware wiring into request handling -- OPS-14d-c.
- End-to-end test against a live request pipeline -- OPS-14d-d.
