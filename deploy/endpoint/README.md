# Endpoint Deployment

This directory contains the Docker Compose stack to run `alfred-coo-svc` in **endpoint** mode.

## Prerequisites
- Fresh Debian (or compatible) VM with at least 2 CPU, 4 GB RAM, and Docker Engine installed (the bootstrap script handles installation).
- Registration token generated via hub (`mcctl token create --site <site> --ttl 15m`).

## Quick start (bootstrap)
```bash
chmod +x bootstrap.sh
./bootstrap.sh
```
The script will:
1. Install Docker if missing.
2. Populate `.env` from `.env.template` (you need to replace `<<PASTE_REGISTRATION_TOKEN_HERE>>`).
3. Pull the latest `salucallc/alfred-coo-svc` image.
4. Start the compose stack.
5. Wait for registration and heartbeat (logs show `registered` and `heartbeat sent`).

## Manual steps
If you prefer to run manually:
1. Edit `.env` with your registration token and `HUB_URL`.
2. `docker compose -f deploy/endpoint/docker-compose.yml up -d`
3. Verify registration: `docker logs alfred-coo-endpoint` should show a successful `POST /v1/fleet/register` response.

## Cleanup
```bash
docker compose -f deploy/endpoint/docker-compose.yml down -v
```
