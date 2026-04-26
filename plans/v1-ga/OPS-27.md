# OPS-27: Add restic restore flow

## Target paths
- deploy/appliance/restic/restore.sh
- scripts/mc_restore.sh
- tests/test_restic_restore.sh
- plans/v1-ga/OPS-27.md

## Acceptance criteria
- Tampered volume recovers; smoke_test.sh passes after.

## Verification approach
- Execute `./scripts/mc_restore.sh restore --snapshot <id>` against a known snapshot.
- Run `./tests/test_restic_restore.sh` which invokes the restore and then runs `smoke_test.sh`.
- Both commands must exit with status 0, confirming volume restoration and successful smoke test.

## Risks
- Restic password/environment variables must be correctly configured; otherwise restore will fail.
- The test assumes `smoke_test.sh` is present and functional.
