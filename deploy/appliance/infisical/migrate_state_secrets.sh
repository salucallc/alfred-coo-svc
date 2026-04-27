#!/usr/bin/env bash
# migrate_state_secrets.sh
# This script migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

# Handle dry-run flag
DRY_RUN=false
if [[ "$#" -ge 1 && "$1" == "--dry-run" ]]; then
  DRY_RUN=true
  shift
fi

# Environment variables for Infisical (can be overridden)
INFISICAL_ENV="${INFISICAL_ENV:-default}"
INFISICAL_PATH="${INFISICAL_PATH:-/}"

STATE_DIR="./state/secrets"
TARGET_DIR="/app/infisical/secrets"

if [[ ! -d "$STATE_DIR" ]]; then
  echo "State directory $STATE_DIR does not exist. Exiting."
  exit 0
fi

mkdir -p "$TARGET_DIR"

# Determine expected secret files (all files in STATE_DIR)
expected_files=()
while IFS= read -r -d $'\0' file; do
  expected_files+=("$(basename "$file")")
done < <(find "$STATE_DIR" -type f -print0)

# Verify all expected secrets are present
for secret in "${expected_files[@]}"; do
  if [[ ! -f "$STATE_DIR/$secret" ]]; then
    echo "ERROR: missing secret $secret" >&2
    exit 1
  fi
done

# Process each secret
for secret in "${expected_files[@]}"; do
  secret_path="$STATE_DIR/$secret"
  secret_value="$(cat "$secret_path")"
  cmd="infisical-cli secret set --env=\"$INFISICAL_ENV\" --path=\"$INFISICAL_PATH\" $secret=\"$secret_value\""
  if $DRY_RUN; then
    echo "$cmd"
  else
    eval "$cmd"
  fi
done

# Secure the original secret files
chmod 000 -R "$STATE_DIR"

echo "Migration completed. Original state secrets are now chmod 000."
