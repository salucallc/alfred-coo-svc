# F19A-d: End-to-end test: mcctl push-policy -> hub -> version row created

**Linear:** SAL-3263

**Parent:** F19A (SAL-3070) -- see `plans/v1-ga/F19A-d.md`
**Wave:** 3
**Auto-dispatchable:** yes

## Context

End-to-end smoke test that exercises the full F19A path: spin up the FastAPI test client, run `mcctl push-policy` against it as an in-process call (or use a fixture-mounted ASGI test transport), then assert that a row in `fleet_policy_versions` exists with the expected sha256.

Depends on F19A-a, F19A-b, F19A-c all being merged.

## APE/V Acceptance (machine-checkable)

1. File `tests/test_e2e_push_policy.py` exists with the test `test_push_policy_creates_version_row`.
2. The test asserts a row exists in `fleet_policy_versions` after the push, with `sha256` matching the helper's computed hash and `name`/`version` matching the manifest.
3. `pytest tests/test_e2e_push_policy.py -v` exits 0.

## Out of scope

- Persona push (sibling F19B will get its own e2e).
- Heartbeat reconciliation (also F19B).
