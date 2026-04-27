# OPS-08: Migrate state secrets to Infisical

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/infisical/migrate_state_secrets.sh
- deploy/appliance/infisical/MIGRATION.md
- plans/v1-ga/OPS-08.md

## Acceptance criteria
Delete state dir, restart -> all services healthy; secrets visible in Infisical UI; on-disk files chmod 000

## Verification approach
- Run the init container to import secrets.
- Delete `./state/secrets` and restart services.
- Confirm services start healthy and secrets appear in Infisical UI.
- Verify the on‑disk files have mode `000`.

## Risks
- Potential temporary unavailability during migration.
- Incorrect permissions could expose secrets.
- Script failures may leave services in inconsistent state.
