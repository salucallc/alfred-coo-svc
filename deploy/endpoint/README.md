# Deploying an Alfred COoSVC Endpoint

This guide walks through setting up a fresh Debian VM as an **endpoint** for the fleet.

## Prerequisites
- Debian 12 (or any recent Debian) with Docker Engine installed.
- Network access to the hub on outbound port 443.
- A one‑time `registration_token` from the hub admin (see hub UI or `mcctl token create`).

## Steps
1. **Clone the repository**
   ```bash
   git clone https://github.com/salucallc/alfred-coo-svc.git
   cd alfred-coo-svc
   ```
2. **Create the compose directory**
   ```bash
   mkdir -p deploy/endpoint
   cp -r docs/endpoint_template/* deploy/endpoint/   # if you have templates
   ```
3. **Edit `.env.template`**
   Replace `YOUR_REGISTRATION_TOKEN_HERE` with the token you obtained.
   Adjust `HUB_URL` if your hub runs on a custom domain.
4. **Start the services**
   ```bash
   cd deploy/endpoint
   docker compose up -d
   ```
5. **Verify registration**
   Check the logs of `alfred-coo-svc-endpoint`:
   ```bash
   docker logs -f alfred-coo-svc-endpoint
   ```
   You should see a successful `201 Created` response and periodic heartbeats.

## Cleanup
```bash
docker compose down
rm -rf data
```

The endpoint runs headless – no UI, no Postgres, no portal. It registers once, then continuously sends heartbeats and syncs memory via `soul-lite`.
