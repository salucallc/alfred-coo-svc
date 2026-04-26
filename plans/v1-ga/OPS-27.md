# OPS-27: Restore flow

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/restic/restore.sh
- scripts/mc_restore.sh
- tests/test_restic_restore.sh
- plans/v1-ga/OPS-27.md

## Acceptance criteria
- Tampered volume recovers; smoke_test.sh passes after.

## Verification approach
- Execute `./scripts/mc_restore.sh <snapshot-id>` against a known Restic snapshot.
- Run `./smoke_test.sh` after restore; ensure it returns exit code 0.

## Risks
- Restic repository password must be correctly derived from KEK; if misconfigured restore will fail.
- The placeholder test does not verify actual data integrity; real CI will need proper snapshot.
