#!/usr/bin/env bash
# Wrapper for ./mc.sh restore command.
# Delegates to the restic restore script.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/appliance/restic" && pwd)
"$SCRIPT_DIR/restore.sh" "$@"
