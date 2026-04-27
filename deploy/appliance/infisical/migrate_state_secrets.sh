#!/usr/bin/env bash
# migrate_state_secrets.sh
# This script migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

# Parse arguments for dry-run flag
DRY_RUN=false
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    DRY_RUN=true
  fi
done

# Source migration manifest if present (expects export statements for ENV and PATH)
MANIFEST="deploy/appliance/infisical/MIGRATION.md"
if [ -f "$MANIFEST" ]; then
  # shellcheck source=/dev/null
  source "$MANIFEST"
fi

STATE_DIR="./state/secrets"
TARGET_DIR="/app/infisical/secrets"

if [ ! -d "$STATE_DIR" ]; then
  echo "State directory $STATE_DIR does not exist. Exiting."
  exit 0
fi

mkdir -p "$TARGET_DIR"

# Iterate over secret files and push to Infisical
for file in "$STATE_DIR"/*; do
  [ -e "$file" ] || continue
  secret_name=$(basename "$file")
  secret_value=$(cat "$file")
  if [ "$DRY_RUN" = "true" ]; then
    echo "infisical-cli secret set --env=$INFISICAL_ENV --path=$INFISICAL_PATH $secret_name=\"$secret_value\""
  else
    if [ -z "$secret_value" ]; then
      echo "ERROR: missing secret $secret_name" >&2
      exit 1
    fi
    infisical-cli secret set --env=$INFISICAL_ENV --path=$INFISICAL_PATH $secret_name="$secret_value"
  fi
done

# Secure the original secret files
chmod 000 -R "$STATE_DIR"

echo "Migration completed. Original state secrets are now chmod 000."
