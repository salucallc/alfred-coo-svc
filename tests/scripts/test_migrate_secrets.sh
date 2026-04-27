#!/usr/bin/env bash
set -euo pipefail

# Setup fixture directory with mock secret files
mkdir -p state/secrets
echo "value1" > state/secrets/secret1
echo "value2" > state/secrets/secret2

# Dry-run test: capture output and verify expected commands are echoed
dry_output=$(bash deploy/appliance/infisical/migrate_state_secrets.sh --dry-run)

echo "$dry_output" | grep -q "infisical-cli secret set"
echo "$dry_output" | grep -q "secret1"
echo "$dry_output" | grep -q "secret2"

# Ensure dry-run exits with code 0
bash deploy/appliance/infisical/migrate_state_secrets.sh --dry-run > /dev/null

# Negative test: remove a secret file and expect error
rm state/secrets/secret2
if bash deploy/appliance/infisical/migrate_state_secrets.sh; then
  echo "Expected failure but script succeeded"
  exit 1
else
  err_output=$(bash deploy/appliance/infisical/migrate_state_secrets.sh 2>&1 || true)
  echo "$err_output" | grep -q "ERROR: missing secret"
fi
