# Migration of Legacy State Secrets

## Overview
The appliance historically stored secrets under `./state/secrets/`. With the introduction of Infisical as the centralized secret vault, these on‑disk secrets must be migrated and then disabled.

## Migration Procedure
1. The `state-migrator` one‑off service runs the `migrate_state_secrets.sh` script on first boot.
2. The script imports all files from `/state/secrets` into Infisical using the appropriate CLI (implementation placeholder).
3. After a successful import, the script chmods the directory to `000` so the files are invisible but recoverable.

## Verification
- After the service completes, the secrets should appear in the Infisical UI.
- The local `./state/secrets` directory should have permissions `000`.
- Restarting the appliance should not re‑expose the secrets.

## Rollback
If migration fails, the directory remains readable. Inspect logs from the `state-migrator` container and retry.
