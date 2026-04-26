#!/usr/bin/env bash
set -euo pipefail

# Ensure script runs from its containing directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}") && pwd)"
cd "$SCRIPT_DIR"

# 1. Install Docker if not present
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found, installing..."
  sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg lsb-release
  curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io
  sudo usermod -aG docker $USER
  newgrp docker <<'EOF'
EOF
fi

# 2. Prepare .env from template
ENV_FILE=".env"
if [[ -f "$ENV_FILE" ]]; then
  echo ".env already exists, skipping generation"
else
  if [[ -f ".env.template" ]]; then
    cp .env.template "$ENV_FILE"
    echo "Created .env from template. Please edit the registration token."
  else
    echo "Error: .env.template not found"
    exit 1
  fi
fi

# 3. Pull image and start compose
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d

# 4. Wait for registration and heartbeat (max 10 minutes)
MAX_TIME=$((10 * 60))
INTERVAL=10
ELAPSED=0
while (( ELAPSED < MAX_TIME )); do
  LOG=$(docker logs alfred-coo-endpoint 2>/dev/null || true)
  if echo "$LOG" | grep -q "registered" && echo "$LOG" | grep -q "heartbeat"; then
    echo "Endpoint successfully registered and sending heartbeats."
    exit 0
  fi
  sleep $INTERVAL
  ((ELAPSED+=INTERVAL))
done

echo "Timeout waiting for endpoint registration/heartbeat. Check logs manually: docker logs alfred-coo-endpoint"
exit 1
