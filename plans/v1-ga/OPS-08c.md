# OPS-08C: Migrate state secrets to Infisical

## Target paths
- deploy/appliance/infisical/migrate_state_secrets.sh
- tests/scripts/test_migrate_secrets.sh
- plans/v1-ga/OPS-08c.md

## Acceptance criteria
**A — Action:**

1. Open `deploy/appliance/infisical/migrate_state_secrets.sh`.
2. For every line matching `# Placeholder: infisical-cli secret set`, replace with a real `infisical-cli secret set --env=<env> --path=<path> <KEY>="<VALUE>"` invocation, using exported env-var names sourced from the manifest at `deploy/appliance/infisical/MIGRATION.md` (or sibling manifest if newer).
3. Add a `--dry-run` flag at the top of the script. When set, the script must echo every intended `infisical-cli` invocation and exit 0 without calling the CLI.
4. On real run, the script must exit 0 when all expected secrets are present in the source state dir, and exit non-zero with a clear `ERROR: missing secret <NAME>` message when any secret is absent.
5. Add `tests/scripts/test_migrate_secrets.sh` that:
   * Stages a fixture `./state/secrets/` directory with mock files.
   * Runs the migration script with `--dry-run` and asserts every expected `infisical-cli secret set` line is echoed.
   * Asserts exit code 0.
   * Runs again with one fixture file removed and asserts non-zero exit + error message.

**P — Plan:**

* Read `migrate_state_secrets.sh` and `MIGRATION.md` to enumerate the expected secret keys.
* Implement `--dry-run` branch first; verify echoed commands match manifest.
* Replace placeholders with real invocations gated behind a `if [ "$DRY_RUN" != "true" ]` block.
* Author the test script using `bash -e` and assert via `grep -q` against captured stdout.

**E — Evidence:**

* `git diff` showing every placeholder line replaced.
* `bash deploy/appliance/infisical/migrate_state_secrets.sh --dry-run` output captured to evidence log; exit code 0.
* `bash tests/scripts/test_migrate_secrets.sh` output captured; exit code 0.
* Negative test (one fixture removed): captured stderr containing `ERROR: missing secret`, non-zero exit code.

**V — Verification (machine-checkable):**

1. `grep -c "# Placeholder: infisical-cli" deploy/appliance/infisical/migrate_state_secrets.sh` returns `0`.
2. `grep -c "infisical-cli secret set" deploy/appliance/infisical/migrate_state_secrets.sh` returns the count of secrets in `MIGRATION.md`.
3. `bash deploy/appliance/infisical/migrate_state_secrets.sh --dry-run` exits 0.
4. `bash tests/scripts/test_migrate_secrets.sh` exits 0.
5. PR includes both files in `git diff --name-only`.
