# OPS-08: Migrate state secrets to Infisical

## Target paths
- `deploy/appliance/docker-compose.yml`
- `deploy/appliance/infisical/migrate_state_secrets.sh`
- `deploy/appliance/infisical/MIGRATION.md`
- `plans/v1-ga/OPS-08.md`

## Acceptance criteria
- Delete state dir, restart -> all services healthy; secrets visible in infisical UI; on-disk files chmod 000

## Verification approach
Manual verification: after deployment, delete `./state/secrets/`, restart services, verify health checks pass, confirm secrets visible in Infisical UI, and check file permissions are `000`.

## Risks
- Potential service downtime during secret import.
- Incorrect permission setting could lock out legitimate access.
- Dependency on OPS-06 and OPS-07 for Infisical integration.
