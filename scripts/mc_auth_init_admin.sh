#!/usr/bin/env bash
# Initialize admin user for Authelia after wizard screen 8
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <admin-passphrase>"
  exit 1
fi

PASS="$1"
# Assuming mc.sh has a subcommand to add admin; placeholder implementation
./mc.sh auth add-admin --passphrase "${PASS}"

# Verify admin was added
if ./mc.sh auth list-users | grep -q "admin"; then
  echo "Admin user created successfully."
else
  echo "Failed to create admin user." >&2
  exit 1
fi
