#!/usr/bin/env bash
# migrate_state_secrets.sh
# This script migrates legacy state secrets to Infisical and secures the original files.

set -euo pipefail

# OPS-08 placeholder guard.
# This script was merged as scaffolding only (PR #166). The Infisical CLI
# integration, secret-push loop, and verification steps are NOT wired.
# Refuse to execute so accidental runs can't half-migrate state or chmod 000
# the originals before they're actually copied anywhere.
#
# The structure below (STATE_DIR / TARGET_DIR / loop / chmod) is preserved
# as a design artifact for the OPS-08c child ticket to replace.
echo "OPS-08 placeholder -- Infisical client not wired. See SAL-2641 and child OPS-08c. Refusing to run." >&2
exit 1

# --- Scaffolding below this line is intentionally unreachable. ---
# It documents the intended shape of the real migration for OPS-08c.

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
