# F19A-b: Hub handler POST /v1/fleet/policy-bundles + fleet_policy_versions row

**Linear:** SAL-3261

**Parent:** F19A (SAL-3070) -- see `plans/v1-ga/F19A-b.md`
**Wave:** 3
**Auto-dispatchable:** yes

## Context

Server-side endpoint that receives a policy bundle upload from `mcctl push-policy` and persists a row in `fleet_policy_versions`. Mirror schema choices from any pre-existing `fleet_persona_versions` table; if none, this ticket creates the canonical pattern.

Body shape: `{name: str, version: str, sha256: str, payload_b64: str}`.
Response: 201 with `{id, name, version, sha256, uploaded_at}` on success; 409 on duplicate `(name, version)`.

Child of SAL-3070 -- depends on F19A-a only for the test fixtures (sha256 helper).

## APE/V Acceptance (machine-checkable)

1. Hub route `POST /v1/fleet/policy-bundles` is registered in the FastAPI router and reachable in OpenAPI (`/openapi.json` contains the path).
2. DB migration file under `migrations/` (or alembic equivalent) creates the `fleet_policy_versions` table with columns `id, name, version, sha256, uploaded_at, uploaded_by`.
3. File `tests/test_fleet_policy_bundles_endpoint.py` exists with tests `test_policy_bundle_create_201` and `test_policy_bundle_duplicate_409`.
4. `pytest tests/test_fleet_policy_bundles_endpoint.py -v` exits 0.

## Out of scope

- CLI subcommand -- F19A-c.
- End-to-end wire test -- F19A-d.
