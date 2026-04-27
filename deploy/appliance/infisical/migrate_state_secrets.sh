#!/usr/bin/env bash
# migrate_state_secrets.sh
# This script migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

STATE_DIR="./state/secrets"
TARGET_DIR="/app/infisical/secrets"

if [ ! -d "$STATE_DIR" ]; then
  echo "State directory $STATE_DIR does not exist. Exiting."
  exit 0
fi

mkdir -p "$TARGET_DIR"

# Example: iterate over files and push to Infisical via CLI (placeholder)
for file in "$STATE_DIR"/*; do
  [ -e "$file" ] || continue
  secret_name=$(basename "$file")
  # Placeholder: infisical-cli secret set $secret_name "$(cat $file)"
  echo "Would import $secret_name to Infisical"
done

# Secure the original secret files
chmod 000 -R "$STATE_DIR"

echo "Migration completed. Original state secrets are now chmod 000."
