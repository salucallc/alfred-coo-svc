# OPS-14d-a: TTL constant + JWT iat claim shape

**Linear:** SAL-3272

**Parent:** OPS-14d (SAL-3038) -- see `plans/v1-ga/OPS-14d.md`
**Wave:** 3
**Auto-dispatchable:** yes (no human-assigned label)

## Context

OPS-14d enforces a hard 24h TTL on scoped OAuth2 tokens by reading the JWT `iat` (issued-at) claim and rejecting any token where `now_unix - iat > 86400`. This child establishes the foundational primitives: the TTL constant and the typed claim shape used by the validator (sibling OPS-14d-b) and the middleware wiring (sibling OPS-14d-c).

PR #214 has prior art on this surface; if it is still open at dispatch time, prefer extending its file rather than re-creating, but only the scope of THIS child should land in this child's PR.

Child of SAL-3038 -- siblings: OPS-14d-b (validator), OPS-14d-c (middleware wiring), OPS-14d-d (e2e test).

## Target paths

- `src/alfred_coo/auth/ttl_validator.py` (new or existing)

## APE/V Acceptance (machine-checkable)

1. File `src/alfred_coo/auth/ttl_validator.py` exists.
2. `grep -E "TOKEN_TTL_SECONDS\s*=\s*86400" src/alfred_coo/auth/ttl_validator.py` returns one match (the TTL constant, exactly 86400 seconds = 24h).
3. The module exports a typed shape (TypedDict, dataclass, or `iat: int | None` parameter signature) representing the `iat` claim such that `python -c "from alfred_coo.auth.ttl_validator import TOKEN_TTL_SECONDS; assert TOKEN_TTL_SECONDS == 86400"` exits 0.

## Out of scope

- Validator function logic -- OPS-14d-b.
- Middleware wiring -- OPS-14d-c.
- End-to-end test -- OPS-14d-d.
