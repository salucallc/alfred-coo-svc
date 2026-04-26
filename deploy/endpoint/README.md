# Endpoint Deployment

This guide walks through deploying the `alfred-coo-svc` in **endpoint** mode on a fresh Debian VM.

## Prerequisites
- Docker & Docker Compose installed
- Network access to the hub URL

## Steps
1. Clone the repository and navigate to `deploy/endpoint`.
2. Copy the `.env.template` to `.env` and fill in `REGISTRATION_TOKEN` and `HUB_URL`.
3. Run `docker compose up -d`.
4. Verify the endpoint registers and heartbeats appear on the hub (use `mcctl endpoint list`).
