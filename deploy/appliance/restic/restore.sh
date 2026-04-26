#!/usr/bin/env bash
# Restore a restic snapshot for the appliance volume.
# Usage: ./restore.sh --snapshot <snapshot-id>
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 --snapshot <snapshot-id>"
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot)
      SNAPSHOT_ID="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1"
      exit 1
      ;;
  esac
done

# Assume RESTIC_REPOSITORY and RESTIC_PASSWORD are set in the environment.
restic restore "$SNAPSHOT_ID" --target / --host "$(hostname)" --path / --verbose
