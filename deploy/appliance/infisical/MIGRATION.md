# Migration of State Secrets to Infisical (OPS‑08)

## Overview
Legacy secrets stored in `./state/secrets/` need to be migrated to Infisical for centralized management while keeping a recoverable backup on‑disk.

## Steps
1. On first container start, the **init** container runs `migrate_state_secrets.sh`.
2. The script imports each file into Infisical (using the Infisical CLI – command omitted for security).
3. After successful import, the script sets the original directory permissions to `000` to prevent accidental reads.
4. Services are restarted; they read secrets from Infisical via environment variables.

## Verification
- Delete the `./state/secrets/` directory manually and restart the stack; all services should start healthy.
- Confirm the secrets appear in the Infisical UI.
- Ensure the on‑disk `./state/secrets/` files have mode `000`.
