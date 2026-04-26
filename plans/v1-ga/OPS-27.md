# OPS-27: Add Restic restore flow

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/restic/restore.sh
- scripts/mc_restore.sh
- tests/test_restic_restore.sh
- plans/v1-ga/OPS-27.md

## Acceptance criteria
```
Tampered volume recovers; smoke_test.sh passes after
```

## Verification approach
- Execute `./scripts/mc_restore.sh <snapshot-id>` against a tampered volume.
- Run the existing `smoke_test.sh` (or the newly added test) to ensure the system recovers and all health checks pass.

## Risks
- Restic repo configuration may be missing in CI; ensure environment variables `RESTIC_REPOSITORY` and `RESTIC_PASSWORD` are set.
- The restore target path must not overwrite critical runtime data; using `/var/lib/restore-target` keeps it isolated.
