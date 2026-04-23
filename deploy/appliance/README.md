# Mission Control Appliance — Bundle

Single-host Saluca Mission Control stack behind a Caddy reverse proxy. Brings up
Alfred portal, soul-svc memory graph, COO orchestration daemon, four MCP tool
services, Open WebUI chat, and Postgres on one machine via `docker compose up -d`.

## What you get

| Service | Role | Internal path |
|---|---|---|
| caddy | Reverse proxy, ACME TLS | `http(s)://${APPLIANCE_HOSTNAME}` |
| alfred-portal | Next.js dashboard + setup wizard | `/` |
| soul-svc | Memory graph API | `/soul/` |
| alfred-coo-svc | COO orchestration daemon | internal only |
| mcp-github | GitHub MCP tool service | `/mcp/github/` |
| mcp-slack | Slack MCP tool service | `/mcp/slack/` |
| mcp-linear | Linear MCP tool service | `/mcp/linear/` |
| mcp-notion | Notion MCP tool service | `/mcp/notion/` |
| open-webui | Chat UI | `/chat/` |
| postgres | Backing DB for portal + soul-svc | internal only |

## Prerequisites

- Docker 24+ and Docker Compose v2
- 8 GB RAM minimum (16 GB recommended if running Ollama on the same host)
- Outbound network access to:
  - `ghcr.io` (image pulls)
  - Your Ollama endpoint (default: host port 11434 via `host.docker.internal`)
  - `acme-v02.api.letsencrypt.org` (only if `APPLIANCE_HOSTNAME` is a real FQDN)

## Quick start

```bash
cd deploy/appliance

# 1. Copy template and fill required secrets
cp .env.template .env

# Generate Postgres + soul-svc root key
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))" >> .env
python -c "import secrets; print('SOUL_API_KEY_ROOT=sk_soul_root_' + secrets.token_urlsafe(32))" >> .env

# Set these in .env before continuing:
#   SOUL_API_KEY   — use SOUL_API_KEY_ROOT value for initial setup; rotate after wizard
#   GITHUB_TOKEN   — GitHub PAT (scopes: repo, read:org, workflow)
#   SLACK_BOT_TOKEN — Slack xoxb token
#   SLACK_TEAM_ID   — Slack workspace ID (starts with T)
#   LINEAR_API_KEY  — Linear API key
#   NOTION_API_KEY  — Notion integration token

# 2. Bring up the stack
docker compose up -d

# 3. Verify health
./smoke_test.sh
```

The first run pulls ~10 images and seeds Postgres. Allow 90-120 seconds for every
service to become healthy.

Open `http://localhost` (or your `APPLIANCE_HOSTNAME`) in a browser. The
first-boot wizard will walk you through license acceptance, tenant setup, base
config, Asphodel PQ posture, CoS interview, fleet proposal, and spin-up.

## Configuration

All configuration lives in `.env`. Re-run `docker compose up -d` to apply changes.

### Making the appliance publicly reachable

1. Point a DNS A record at the machine's public IP.
2. Set `APPLIANCE_HOSTNAME=your.domain` in `.env`.
3. Ensure ports 80/443 are open inbound.
4. `docker compose up -d caddy` — Caddy auto-issues a Let's Encrypt cert.

### MCP services

Each of the four curated MCP tool services listens on its own port inside the
appliance network and is reachable via Caddy:

| Service | Internal address | External path |
|---|---|---|
| mcp-github | `mcp-github:8203` | `/mcp/github/` |
| mcp-slack | `mcp-slack:8201` | `/mcp/slack/` |
| mcp-linear | `mcp-linear:8208` | `/mcp/linear/` |
| mcp-notion | `mcp-notion:8212` | `/mcp/notion/` |

All four use the `ghcr.io/salucallc/mcp-gateway-node:latest` base image with
[supergateway](https://github.com/supercorp-ai/supergateway) as the stdio bridge.

### COO Daemon

`alfred-coo-svc` is the orchestration daemon. It connects to soul-svc and the
four MCP services to run autonomous COO workflows. Configure:

- `SOUL_API_KEY` — initially set to the same value as `SOUL_API_KEY_ROOT`; after
  the wizard completes, mint a scoped key and rotate.
- `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `LINEAR_API_KEY` — same tokens as the
  corresponding MCP services.

## Troubleshooting

### Services stuck in `starting`

```bash
docker compose ps
docker compose logs soul-svc | tail -40
```

Most common cause: a required env var missing in `.env` (the service name and the
missing var appear in the error log).

### Open WebUI can't reach Ollama

The default `OLLAMA_BASE_URL=http://host.docker.internal:11434` assumes Ollama
runs on the host. On Linux with Docker Engine (not Desktop), add
`--add-host host.docker.internal:host-gateway` to the open-webui service, or set
`OLLAMA_BASE_URL` to the remote URL.

### MCP service unhealthy

```bash
docker compose logs mcp-github | tail -20
```

Each MCP service downloads its npm package on first start; allow 30-60 s on a
fresh pull. If the healthcheck fires before the package downloads, it will restart
once and recover.

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

- Wizard Screens 1-7: `salucallc/alfred-portal/src/app/wizard/`
- soul-svc API reference: `salucallc/soul-svc/README.md`
- MCP gateway source: `salucallc/mcp-gateway`
- Architecture overview: `ARCH.md`
