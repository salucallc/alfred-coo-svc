#!/usr/bin/env bash
# migrate_state_secrets.sh
# Migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

# Parse dry-run flag
DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# Directory containing legacy secrets
STATE_DIR="./state/secrets"
if [ ! -d "$STATE_DIR" ]; then
  echo "State directory $STATE_DIR does not exist. Exiting."
  exit 0
fi

# Expected number of secrets (optional, can be set via env var)
EXPECTED_COUNT=${EXPECTED_COUNT:-0}

# Gather secret files
mapfile -t files < <(find "$STATE_DIR" -type f)
actual_count="${#files[@]}"

if [ "$EXPECTED_COUNT" -ne 0 ] && [ "$actual_count" -lt "$EXPECTED_COUNT" ]; then
  echo "ERROR: missing secret" >&2
  exit 1
fi

# Ensure target directory exists (placeholder, not used by Infisical directly)
mkdir -p "/app/infisical/secrets"

for file in "${files[@]}"; do
  secret_name=$(basename "$file")
  secret_value=$(cat "$file")
  cmd="infisical-cli secret set --env=dev --path=/app/infisical/secrets $secret_name=\"$secret_value\""
  if $DRY_RUN; then
    echo "$cmd"
  else
    eval "$cmd"
  fi
done

# Secure the original secret files
chmod 000 -R "$STATE_DIR"
echo "Migration completed. Original state secrets are now chmod 000."
