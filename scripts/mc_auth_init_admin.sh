#!/usr/bin/env bash
set -euo pipefail

# Usage: ./mc_auth_init_admin.sh <passphrase>
PASS="${1:-${MC_ADMIN_PASSPHRASE:-}}"
if [[ -z "$PASS" ]]; then
  echo "Error: passphrase not provided." >&2
  exit 1
fi

# Initialize admin user in Authelia via mc tool
./mc.sh auth init-admin --passphrase "$PASS"
