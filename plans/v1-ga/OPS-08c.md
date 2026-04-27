# OPS-08C: INFISICAL migration implementation

## Target paths
- deploy/appliance/infisical/migrate_state_secrets.sh
- tests/scripts/test_migrate_secrets.sh
- plans/v1-ga/OPS-08c.md

## Acceptance criteria
* Replace all `# Placeholder: infisical-cli` lines with real `infisical-cli secret set --env=<env> --path=<path> <KEY>="<VALUE>"` invocations.
* Add `--dry-run` mode that echoes commands and exits 0.
* Script exits 0 on success, non‑zero with `ERROR: missing secret <NAME>` on missing secret.
* Test script `tests/scripts/test_migrate_secrets.sh` validates dry‑run output and failure case.

## Verification approach
* Run `grep -c "# Placeholder: infisical-cli"` – expect 0.
* Run `grep -c "infisical-cli secret set"` – expect count matches secrets in `MIGRATION.md`.
* Execute script with `--dry-run` – expect exit 0.
* Execute test script – expect exit 0.
* Ensure both new files appear in `git diff --name-only`.

## Risks
* Misconfiguration of `INFISICAL_ENV` or `INFISICAL_PATH` may cause runtime failures.
* Deleting original secret files before successful migration could lead to data loss – script secures them only after successful push.
