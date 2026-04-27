#!/usr/bin/env bash
set -euo pipefail

# Prepare fixture directory
FIXTURE_DIR="./state/secrets"
mkdir -p "$FIXTURE_DIR"

# Create mock secret files
echo "value1" > "$FIXTURE_DIR/secret1"
echo "value2" > "$FIXTURE_DIR/secret2"

# Export env vars used by the migration script
export INFISICAL_ENV="testenv"
export INFISICAL_PATH="/test/path"

# Run migration script in dry‑run mode and capture output
output=$(bash ../../deploy/appliance/infisical/migrate_state_secrets.sh --dry-run)

# Assert both expected commands are echoed
echo "$output" | grep -q 'infisical-cli secret set --env="testenv" --path="/test/path" secret1="value1"'
echo "$output" | grep -q 'infisical-cli secret set --env="testenv" --path="/test/path" secret2="value2"'

# Now test failure when a secret is missing
rm "$FIXTURE_DIR/secret2"
if bash ../../deploy/appliance/infisical/migrate_state_secrets.sh; then
  echo "Expected failure due to missing secret" >&2
  exit 1
else
  err=$(bash ../../deploy/appliance/infisical/migrate_state_secrets.sh 2>&1 || true)
  echo "$err" | grep -q 'ERROR: missing secret secret2'
fi

echo "All tests passed."
