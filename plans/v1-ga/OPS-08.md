# OPS-08: Migrate `./state/secrets` to Infisical

## Target paths
- `deploy/appliance/docker-compose.yml`
- `deploy/appliance/infisical/migrate_state_secrets.sh`
- `deploy/appliance/infisical/MIGRATION.md`

## Acceptance criteria
Delete state dir, restart -> all services healthy; secrets visible in infisical UI; on-disk files chmod 000

## Verification approach
* Run `docker compose up` on a fresh checkout.
* Ensure the `state-migrator` service runs, uploads secrets, deletes the directory.
* Confirm `docker compose ps` shows all services (except the one‑off migrator) in **running** state.
* Log into Infisical UI and verify the presence of the previously‑present secret keys.

## Risks
* If Infisical service is unavailable, migration will fail – the script exits without deleting files.
* Permissions change may prevent later manual inspection – but directory is removed.

## Notes
The migration is idempotent: re‑running on a host without `./state/secrets` is a no‑op.
