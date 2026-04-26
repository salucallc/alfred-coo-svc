#!/usr/bin/env bash
set -euo pipefail
# Wrapper for the appliance restore command
# Delegates to the internal restore script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}") && pwd)"
$SCRIPT_DIR/../deploy/appliance/restic/restore.sh "$@"
