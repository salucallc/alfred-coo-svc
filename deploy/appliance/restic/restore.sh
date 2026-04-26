#!/usr/bin/env bash
set -euo pipefail
# restore.sh – restore a restic snapshot to the appliance volumes
# Usage: ./restore.sh <snapshot-id>
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <snapshot-id>"
  exit 1
fi
SNAPSHOT_ID="$1"
# Assume restic repository is configured via environment variables (RESTIC_REPOSITORY, RESTIC_PASSWORD)
restic -r "$RESTIC_REPOSITORY" -p "$RESTIC_PASSWORD" restore "$SNAPSHOT_ID" --target /var/lib/restore-target
echo "Restore of snapshot $SNAPSHOT_ID completed."
