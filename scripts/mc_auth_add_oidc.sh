#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <provider> --client-id <client-id> [--client-secret <secret>] [--redirect-uri <uri>]"
  exit 1
fi

PROVIDER="$1"
shift

# Forward all remaining args to the main mc.sh auth command
./mc.sh auth add-oidc "$PROVIDER" "$@"
