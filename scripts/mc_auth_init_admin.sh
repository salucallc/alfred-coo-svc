#!/usr/bin/env bash
# Initialize admin user for Authelia after wizard screen 8
set -euo pipefail

PASSFILE="/etc/authelia/users.yml"
ADMIN_USER="admin"
ADMIN_PASS="${1:-}"  # Passphrase provided as first argument

if [[ -z "$ADMIN_PASS" ]]; then
  echo "Usage: $0 <admin-passphrase>"
  exit 1
fi

# Add admin user to Authelia configuration (simple file backend example)
cat <<EOF >> "$PASSFILE"
${ADMIN_USER}:
  password: "${ADMIN_PASS}"
  display_name: "Administrator"
  groups:
    - "appliance-admin"
EOF

echo "Admin user added to $PASSFILE"
