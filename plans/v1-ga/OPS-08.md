# OPS-08: Migrate state secrets to Infisical

## Target paths
- `deploy/appliance/docker-compose.yml`
- `deploy/appliance/infisical/migrate_state_secrets.sh`
- `deploy/appliance/infisical/MIGRATION.md`
- `plans/v1-ga/OPS-08.md`

## Acceptance criteria
- Delete state dir, restart -> all services healthy, secrets visible in infisical UI; on-disk files chmod 000

## Verification approach
- Run the one‑off `state-migrator` service, then restart the appliance.
- Confirm all services report healthy via healthchecks.
- Verify secrets appear in the Infisical web UI.
- Check that `./state/secrets` on the host has mode `000`.

## Risks
- If the migration script fails, secrets remain on‑disk; ensure logs are captured.
- Deleting the state directory prematurely could break services that expect files; migration runs before services start.
