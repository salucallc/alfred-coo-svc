# Endpoint Deployment

This directory contains the Docker Compose setup for running `alfred-coo-svc` in **endpoint** mode.

## Files
- `docker-compose.yml` – service definitions.
- `.env.template` – template for required environment variables.
- `bootstrap.sh` – helper script to start the services.

## Setup
1. Copy `.env.template` to `.env` and fill in the `REGISTRATION_TOKEN` and `HUB_URL`.
2. Run `./bootstrap.sh`.
3. Verify the containers are up with `docker compose ps`.
4. Check the endpoint registers with the hub (look for heartbeat logs).

The entire process should take less than **10 minutes** on a fresh Debian VM.
