#!/usr/bin/env bash
set -euo pipefail

# Determine repository root
REPO_ROOT=$(git rev-parse --show-toplevel)

# Create temporary fixture directory
FIXTURE_DIR=$(mktemp -d)
STATE_DIR="$FIXTURE_DIR/state/secrets"
mkdir -p "$STATE_DIR"

# Create mock secret files
echo "value1" > "$STATE_DIR/foo.txt"
echo "value2" > "$STATE_DIR/bar.txt"

# Export EXPECTED_COUNT for the migration script
export EXPECTED_COUNT=2

# Dry‑run test: should echo commands and exit 0
output=$("$REPO_ROOT/deploy/appliance/infisical/migrate_state_secrets.sh" --dry-run 2>&1)
# Verify each secret line is echoed
echo "$output" | grep -q 'infisical-cli secret set.*foo.txt="value1"'
echo "$output" | grep -q 'infisical-cli secret set.*bar.txt="value2"'
# Ensure exit code 0
"$REPO_ROOT/deploy/appliance/infisical/migrate_state_secrets.sh" --dry-run

# Negative test: remove one secret file
rm "$STATE_DIR/bar.txt"
# Expect non‑zero exit and error message
if "$REPO_ROOT/deploy/appliance/infisical/migrate_state_secrets.sh" --dry-run; then
  echo "Expected failure due to missing secret"
  exit 1
fi

echo "All tests passed"
