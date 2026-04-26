#!/usr/bin/env bash
set -euo pipefail

# Install Docker if not present
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  apt-get update && apt-get install -y docker.io docker-compose
fi

# Navigate to script directory
cd "$(dirname "${BASH_SOURCE[0]}")"

# Copy env template to .env
cp .env.template .env

# Prompt user to edit .env
read -p "Edit .env now? (y/n) " edit_choice
if [[ "$edit_choice" == "y" ]]; then
  ${EDITOR:-nano} .env
fi

# Start the compose services
docker compose up -d

echo "Endpoint deployed. Check registration on the hub."
