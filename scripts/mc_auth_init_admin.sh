#!/usr/bin/env bash
# Script to initialize the admin user after wizard screen 8
# Usage: ./mc_auth_init_admin.sh <passphrase>

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <admin-passphrase>"
  exit 1
fi

PASS="$1"

# Call the mc tool to create the admin user
./mc.sh auth init-admin --passphrase "$PASS"

# Verify that the admin user appears in the user list
if ./mc.sh auth list-users | grep -q "admin"; then
  echo "Admin user successfully created."
else
  echo "Failed to create admin user."
  exit 1
fi
