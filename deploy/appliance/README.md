# Mission Control Appliance — Bundle

Single-host Saluca Mission Control stack behind a Caddy reverse proxy. Brings up
Alfred portal, soul-svc memory graph, MCP gateway, Open WebUI chat, and Postgres
on one machine via `docker compose up -d`.

## What you get

| Service | Role | URL on default host |
|---|---|---|
| caddy | Reverse proxy, ACME TLS | `http(s)://${APPLIANCE_HOSTNAME}` |
| alfred-portal | Next.js dashboard + setup wizard | `/` |
| soul-svc | Memory graph API | `/api/soul` |
| mcp-gateway | MCP tool routing | `/api/mcp` |
| open-webui | Chat UI | `/chat` |
| postgres | Backing DB for portal + soul-svc | internal only |

## Prerequisites

- Docker 24+ and Docker Compose v2
- 8 GB RAM minimum (16 GB recommended if running Ollama on the same host)
- Outbound network access to:
  - `ghcr.io` (image pulls)
  - Your Ollama endpoint (default: the host's port 11434 via `host.docker.internal`)
  - `acme-v02.api.letsencrypt.org` (only if you set `APPLIANCE_HOSTNAME` to a real FQDN)

## Quick start

```bash
cd deploy/appliance

# 1. Generate secrets
cp .env.template .env
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))" >> .env
python -c "import secrets; print('SOUL_API_KEY_ROOT=sk_soul_root_' + secrets.token_urlsafe(32))" >> .env

# 2. Bring up the stack
docker compose up -d

# 3. Verify health
./smoke_test.sh
```

The first run pulls 6 images (~1.5 GB total) and seeds Postgres. Allow 60-90
seconds for every service to become healthy.

Open `http://localhost` (or your `APPLIANCE_HOSTNAME`) in a browser. The
first-boot wizard will walk you through license acceptance → tenant setup →
base config → Asphodel PQ posture → CoS interview → fleet proposal → spin-up.

## Configuration

All configuration lives in `.env`. Re-run `docker compose up -d` to apply changes
(Caddy and soul-svc pick up env at restart).

### Making the appliance publicly reachable

1. Point a DNS A record at the machine's public IP.
2. Set `APPLIANCE_HOSTNAME=your.domain` in `.env`.
3. Ensure ports 80/443 are open inbound.
4. `docker compose up -d caddy` — Caddy will auto-issue a Let's Encrypt cert.

## Troubleshooting

### Services stuck in `starting`

`docker compose ps` — if `postgres` is healthy but `soul-svc` keeps restarting,
check logs:

```bash
docker compose logs soul-svc | tail -40
```

Most common cause: `SOUL_API_KEY_ROOT` or `POSTGRES_PASSWORD` missing in `.env`.

### Open WebUI can't reach Ollama

The default `OLLAMA_BASE_URL=http://host.docker.internal:11434` assumes:
- Ollama is running on the host machine
- Docker is Docker Desktop or `--add-host host.docker.internal:host-gateway`
  was already baked into Linux Docker Engine (v20.10+)

For a remote Ollama endpoint, set the URL directly (must be reachable from
the appliance network).

### Need a fresh start

```bash
docker compose down -v     # drops all volumes including Postgres + soul data
docker compose up -d
```

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Releases are tagged in git. Pin specific versions in `docker-compose.yml` if you
prefer immutable deploys over `:latest`.

## Related

- Wizard Screens 1-7 live in `salucallc/alfred-portal` under `src/app/wizard/`
- soul-svc API reference: `salucallc/soul-svc/README.md`
- Architecture overview: `ARCH.md` (appliance COO daemon that orchestrates all of this)
