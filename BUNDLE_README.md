# Saluca cockpit bundle

A `docker compose` stack that runs the three services Cristian needs to
work the cockpit from his laptop:

| Service       | Image                                   | Host port | Health             |
| ------------- | --------------------------------------- | --------- | ------------------ |
| `cockpit`     | `ghcr.io/salucallc/alfred-portal:latest`| `3000`    | `GET /api/healthz` |
| `alfred-coo`  | `ghcr.io/salucallc/alfred-coo-svc:latest`| `8091`   | `GET /healthz`     |
| `soul-svc`    | `ghcr.io/salucallc/soul-svc:latest`     | `8080`    | `GET /health`      |

`soul-svc` boots first; `alfred-coo` waits for it to go healthy; `cockpit`
waits for `alfred-coo`. All three restart `unless-stopped` and have
log rotation capped at 5 files of 20 MB each.

## Prerequisites

- Docker Engine 24+ (Docker Desktop on Windows / macOS, or `docker-ce`
  on Linux).
- About 4 GB of free RAM.
- Free TCP ports `3000`, `8080`, `8091` on the host. Override any of
  them in the per-service `.env` files if you need a different mapping.

## First-time setup

```bash
git clone https://github.com/salucallc/alfred-coo-svc
cd alfred-coo-svc

# Per-service env files. The compose file expects both to exist.
cp alfred-coo.env.example alfred-coo.env
cp soul-svc.env.example   soul-svc.env

# ...edit both files. At minimum:
#   alfred-coo.env  -> ANTHROPIC_API_KEY, GITHUB_TOKEN, LINEAR_API_KEY,
#                     SLACK_BOT_TOKEN_ALFRED, SOUL_API_KEY
#   soul-svc.env    -> SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SOUL_API_KEY
#                     (the SOUL_API_KEY value must match alfred-coo.env)

docker compose pull
docker compose up -d
```

Once everything is healthy:

- Open the cockpit at <http://localhost:3000>.
- Daemon health: `curl http://localhost:8091/healthz`.
- Memory graph health: `curl http://localhost:8080/health`.

## Day-to-day

```bash
# tail logs across the stack
docker compose logs -f

# stop everything (state in ./state/alfred-coo + the soul-svc-state
# volume is preserved)
docker compose down

# restart a single service
docker compose restart alfred-coo

# pull newer images and roll forward
docker compose pull
docker compose up -d
```

To wipe soul-svc state (Supabase data is unaffected; only the local
healthcheck DB and harness registry get dropped):

```bash
docker compose down -v
```

## Where state lives

| Path                              | What                                       |
| --------------------------------- | ------------------------------------------ |
| `./state/alfred-coo/`             | Daemon artifacts, workspaces, cron state.  |
| `soul-svc-state` (named volume)   | soul-svc's local SQLite + harness registry.|
| Supabase (cgtuoiggcngldtzfqosm)   | Authoritative memory graph; never local.   |

The `./state/alfred-coo` path matches the layout used by the systemd
unit on Oracle so a future cutover is a `docker stop` away.

## Pinning a specific build

`docker-compose.yml` reads three image-tag knobs from the env files (or
shell). They default to `latest`, but you can pin any of them to a SHA
or branch tag:

```bash
# alfred-coo.env
ALFRED_COO_TAG=sha-abcdef0
# or
ALFRED_COO_TAG=branch-feature-mission-control-v0

# soul-svc.env
SOUL_SVC_TAG=sha-1234abc

# shell or alfred-coo.env
COCKPIT_TAG=sha-9876fed
```

Then re-run `docker compose pull && docker compose up -d`.

## Troubleshooting

- **`alfred-coo` keeps restarting.** Check `docker compose logs alfred-coo`
  for missing env vars. `SOUL_API_KEY`, `ANTHROPIC_API_KEY` and at least one
  Slack/Linear token are required for the persona-driven loops to run; the
  daemon still boots without them but parks in degraded mode.
- **Cockpit shows `Failed to fetch` errors.** The browser hits
  `NEXT_PUBLIC_ALFRED_COO_URL` directly, not through the docker network.
  If you mapped `ALFRED_COO_PORT` to something other than `8091`, also
  set `NEXT_PUBLIC_ALFRED_COO_URL=http://localhost:<your-port>` in
  `alfred-coo.env`.
- **`soul-svc` health is unhealthy on first boot.** It needs a few
  seconds for Supabase auth to round-trip. `docker compose logs soul-svc`
  should show `Application startup complete.` within ~10s.

## Production deployment

This bundle is built for Cristian's daily-driver install. For Oracle /
multi-tenant production, see `deploy/` for the systemd + appliance bundle
flow. The two stacks coexist; the bundle uses `localhost:8091` host
mapping so it never collides with a systemd-managed `alfred-coo` on
`8090`.
