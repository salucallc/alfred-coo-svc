#!/usr/bin/env bash
# Wizard admin initialization script for OPS-15
# Usage: echo '{"passphrase":"YOUR_PASS"}' | ./mc_auth_init_admin.sh
set -euo pipefail

# Read JSON from stdin
read -r payload
# Extract passphrase using jq (assumes jq is available in the container)
passphrase=$(echo "$payload" | jq -r .passphrase)
if [[ -z "$passphrase" || "$passphrase" == "null" ]]; then
  echo "Error: passphrase not provided"
  exit 1
fi

# Run the mc tool to create admin user
# The mc tool expects the passphrase argument after --passphrase
./mc.sh auth add-admin --passphrase "$passphrase"

echo "Admin user initialized successfully."
