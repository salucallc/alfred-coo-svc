# Migration of `./state/secrets` to Infisical (OPS-08)

## Overview
The appliance used to store secret files under `./state/secrets` on the host filesystem. Starting with OPS‑08 we migrate these secrets into the self‑hosted Infisical vault and then disable the on‑disk copies.

## Steps performed by the migration
1. **Container `state-migrator`** runs on first compose up.
2. It reads every file in `/state/secrets`.
3. For each file it calls the Infisical CLI (placeholder in this repo) to create a secret with the same name and content.
4. After all files are uploaded the directory is **chmod 000** (disabled‑but‑recoverable) and then removed.

## Verification
After `docker compose up` completes:
- The `state/secrets` directory no longer exists on the host.
- All services start without error.
- Logging into the Infisical UI shows the uploaded secrets.

## Rollback
If the migration fails, the `state/secrets` directory remains untouched (the script exits early). The `state-migrator` service can be re‑run after fixing any issues.
