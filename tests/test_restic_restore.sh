#!/usr/bin/env bash
# Test that restic restore works and the smoke test passes.

set -euo pipefail

# Simulate a restore with a dummy snapshot ID.
SNAP_ID="dummy-snapshot-id"

echo "Running restic restore simulation..."
scripts/mc_restore.sh restore --snapshot "$SNAP_ID"

# If a smoke_test.sh exists, run it.
if [[ -x ./smoke_test.sh ]]; then
  echo "Running smoke_test.sh..."
  ./smoke_test.sh
fi

echo "Restic restore test completed successfully."
