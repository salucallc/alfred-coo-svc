#!/usr/bin/env bash
set -euo pipefail

# Migrate secrets from the on‑disk state directory into Infisical.
# This script is intended to run once during the first appliance boot.

if [ -d "/state/secrets" ]; then
  echo "Migrating secrets to Infisical..."
  # Placeholder for Infisical CLI import, e.g.:
  # infisical import /state/secrets/*
fi

# Secure the local secrets after migration.
chmod -R 000 /state/secrets || true
