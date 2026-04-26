#!/usr/bin/env bash
set -euo pipefail
if [ -z "${1:-}" ]; then
  echo "Usage: $0 <passphrase>"
  exit 1
fi
PASS="$1"
./mc.sh auth init-admin --passphrase "$PASS"
echo "Admin initialized."
