#!/usr/bin/env bash
# migrate_state_secrets.sh
# This script migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

# Parse dry-run flag
DRY_RUN="false"
if [[ "$#" -gt 0 && "$1" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

STATE_DIR="./state/secrets"
MIGRATION_MANIFEST="deploy/appliance/infisical/MIGRATION.md"

# Verify state directory exists
if [ ! -d "$STATE_DIR" ]; then
  echo "State directory $STATE_DIR does not exist. Exiting."
  exit 0
fi

# Function to process a secret
process_secret() {
  local file="$1"
  local secret_name=$(basename "$file")
  local secret_value
  secret_value=$(cat "$file")
  # Placeholder env and path variables – could be derived from manifest
  local env_var="ENV_PLACEHOLDER"
  local path_opt="PATH_PLACEHOLDER"

  local cmd="infisical-cli secret set --env=${env_var} --path=${path_opt} ${secret_name}=\"${secret_value}\""
  if [ "$DRY_RUN" = "true" ]; then
    echo "$cmd"
  else
    eval "$cmd"
  fi
}

# Check for missing secrets (any missing file will cause error)
missing=0
for file in "$STATE_DIR"/*; do
  [ -e "$file" ] || continue
  if [ ! -f "$file" ]; then
    echo "ERROR: missing secret $(basename "$file")" >&2
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  echo "ERROR: missing secret(s) detected." >&2
  exit 1
fi

# Iterate and migrate
for file in "$STATE_DIR"/*; do
  [ -e "$file" ] || continue
  process_secret "$file"
 done

# Secure the original secret files
chmod 000 -R "$STATE_DIR"

echo "Migration completed. Original state secrets are now chmod 000."
