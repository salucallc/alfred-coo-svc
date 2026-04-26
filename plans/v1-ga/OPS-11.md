# OPS-11: Caddy forward_auth for ops routes

## Target paths
- deploy/appliance/Caddyfile
- deploy/appliance/docker-compose.yml
- plans/v1-ga/OPS-11.md

## Acceptance criteria
- `curl -I /ops/` returns `302`
- `curl` with a valid session cookie returns `200`

## Verification approach
- Run `curl -I http://$APPLIANCE_HOSTNAME/ops/` and verify a 302 redirect to `/auth`.
- Obtain a session cookie via Authelia login, then `curl -b cookie.txt http://$APPLIANCE_HOSTNAME/ops/` and verify a 200 response and successful Grafana UI load.
- Ensure the `forward_auth` directive is present in the Caddyfile and does not interfere with other routes.

## Risks
- Misconfiguration of `forward_auth` could cause authentication loops or block access.
- Authelia downtime would render `/ops/*` inaccessible.
- Path stripping must occur after authentication to preserve correct upstream routing.
