#!/usr/bin/env bash
set -euo pipefail

# Register the minted soulkeys in the allowlist for COO.

DB_CONN="postgres://postgres:postgres@postgres:5432/appliance?search_path=public"

# Services to allow for COO (minimum 4)
allow_services=("coo" "gateway" "proxy" "audit")

for svc in "${allow_services[@]}"; do
  key=$(cat "/state/secrets/soulkey_${svc}" 2>/dev/null || true)
  if [[ -z "${key}" ]]; then
    echo "No soulkey found for ${svc}, skipping."
    continue
  fi
  psql "${DB_CONN}" <<SQL
INSERT INTO _soulkey_allowlist (service, soulkey, allowed_for) 
VALUES ('${svc}', '${key}', 'coo')
ON CONFLICT DO NOTHING;
SQL
  echo "Registered allowlist entry for ${svc}"
done

echo "Allowlist registration completed."
