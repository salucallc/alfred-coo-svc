#!/usr/bin/env bash
set -euo pipefail
# Simple smoke test for the restore flow – expects exit code 0
# In CI we provide a dummy snapshot id "test-snapshot"
./scripts/mc_restore.sh test-snapshot
exit $?
