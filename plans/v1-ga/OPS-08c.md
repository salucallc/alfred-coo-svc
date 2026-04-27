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

## Verification approach
- Run `bash deploy/appliance/infisical/migrate_state_secrets.sh --dry-run` and ensure it exits 0.
- Execute `bash tests/scripts/test_migrate_secrets.sh` and ensure it exits 0.
- Verify that `grep -c "# Placeholder: infisical-cli"` returns `0` and that the count of `infisical-cli secret set` lines matches the number of secrets defined in `MIGRATION.md`.

## Risks
- Assumes the secret manifest format; changes require script updates.
- Dry‑run flag must be kept in sync with real execution path.

## Notes
- Uses environment variable `EXPECTED_COUNT` to enforce missing‑secret detection in tests.
