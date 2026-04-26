#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  cp .env.template .env
  echo "Please edit .env with your registration token and hub URL."
  exit 1
fi

docker compose up -d

echo "Endpoint services started."
