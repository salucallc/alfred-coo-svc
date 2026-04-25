# Infisical Service

This directory contains artifacts for the Infisical secret‑vault service introduced in **SAL‑2637**.

## init.sql
The `init.sql` file creates the `infisical` schema and a minimal `secrets` table. When the Infisical container starts it runs its own migrations; this file ensures the schema is present for the initial deployment and for any manual database inspection.

## Running locally
1. Ensure the `mc-postgres` container is up (defined in `docker‑compose.yml`).
2. Execute the script against the database:
   ```bash
   docker exec -i mc-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB < deploy/appliance/infisical/init.sql
   ```
3. Start the Infisical service (added to `docker‑compose.yml` in a later ticket).

## Verification
- `curl infisical:8080/api/status` should return `{"status":"ok"}`.
- Running `psql` and listing tables in the `infisical` schema should show at least five tables created by Infisical's own migrations.
