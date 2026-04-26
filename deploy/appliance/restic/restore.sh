#!/usr/bin/env bash
# restore.sh - helper for restic restore
# Usage: ./restore.sh <snapshot-id>
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <snapshot-id>"
  exit 1
fi
SNAPSHOT_ID=$1
# Assume restic repo is configured via environment variables
restic restore "$SNAPSHOT_ID" --target / --verbose
