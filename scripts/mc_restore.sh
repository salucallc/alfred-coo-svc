#!/usr/bin/env bash
# Wrapper script for ./mc.sh restore
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <snapshot-id>"
  exit 1
fi
./mc.sh restore --snapshot "$1"
