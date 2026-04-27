#!/usr/bin/env bash
set -euo pipefail

# Determine repository root
REPO_ROOT=$(git rev-parse --show-toplevel)
SCRIPT="$REPO_ROOT/deploy/appliance/infisical/migrate_state_secrets.sh"

# Create temporary fixture directory
TMPDIR=$(mktemp -d)
STATE_DIR="$TMPDIR/state/secrets"
mkdir -p "$STATE_DIR"

# Create mock secret files
echo "valueA" > "$STATE_DIR/SECRET_A"
echo "valueB" > "$STATE_DIR/SECRET_B"

# Export expected env vars for the script (as defined in MIGRATION.md)
export INFISICAL_ENV="test-env"
export INFISICAL_PATH="/test/path"

# Run dry-run and capture output
DRY_OUTPUT=$($SCRIPT --dry-run 2>&1)

# Verify that expected commands are echoed
echo "$DRY_OUTPUT" | grep -q "infisical-cli secret set --env=$INFISICAL_ENV --path=$INFISICAL_PATH SECRET_A=\"valueA\""
echo "$DRY_OUTPUT" | grep -q "infisical-cli secret set --env=$INFISICAL_ENV --path=$INFISICAL_PATH SECRET_B=\"valueB\""

# Ensure exit code was zero (dry-run should not fail)
# The script already exits on error; reaching here means success

# Remove one secret to simulate missing secret
rm "$STATE_DIR/SECRET_B"

# Run real migration, expect failure
set +e
REAL_OUTPUT=$($SCRIPT 2>&1)
EXIT_CODE=$?
set -e
if [ $EXIT_CODE -eq 0 ]; then
  echo "Expected failure due to missing secret, but script exited 0"
  exit 1
fi

echo "$REAL_OUTPUT" | grep -q "ERROR: missing secret SECRET_B"

# Cleanup
chmod -R 777 "$TMPDIR" || true
rm -rf "$TMPDIR"

echo "All tests passed"
