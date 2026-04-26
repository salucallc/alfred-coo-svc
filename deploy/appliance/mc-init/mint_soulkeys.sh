#!/usr/bin/env bash
set -euo pipefail

# mc-init script to mint soulkeys idempotently.
# Writes 6 soulkeys to state/secrets/ and registers them in the DB.

SECRETS_DIR="/state/secrets"
DB_CONN="postgres://postgres:postgres@postgres:5432/appliance?search_path=public"

# Ensure secrets directory exists
mkdir -p "${SECRETS_DIR}"

# Function to generate a soulkey
generate_soulkey() {
  local service=$1
  local sha_tail=$(openssl rand -hex 32 | cut -c1-16)
  echo "sk_agent_appliance_${service}_${sha_tail}"
}

# Mint soulkeys for each service if not already present
services=("coo" "gateway" "proxy" "audit" "metrics" "logging")
for svc in "${services[@]}"; do
  key_file="${SECRETS_DIR}/soulkey_${svc}"
  if [[ -f "${key_file}" ]]; then
    echo "Soulkey for ${svc} already exists, skipping."
    continue
  fi
  key=$(generate_soulkey "${svc}")
  echo "${key}" > "${key_file}"
  chmod 600 "${key_file}"
  echo "Minted soulkey for ${svc}"
done

# Insert records into _soulkeys table (idempotent)
psql "${DB_CONN}" <<SQL
DO $$
BEGIN
  FOR rec IN SELECT service FROM (VALUES
    ('coo'),('gateway'),('proxy'),('audit'),('metrics'),('logging')
  ) AS t(service) LOOP
    IF NOT EXISTS (SELECT 1 FROM _soulkeys WHERE service = rec.service) THEN
      INSERT INTO _soulkeys (service, key_blob) VALUES (rec.service, (SELECT content FROM pg_read_file('${SECRETS_DIR}/soulkey_'||rec.service)));
    END IF;
  END LOOP;
END
$$;
SQL

echo "Soulkey minting completed."
