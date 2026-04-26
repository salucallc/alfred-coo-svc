# SAL-2592: Split docker networks — mc-internal (no internet route) for coo/portal/open-webui/soul; mc-egress for caddy/mcp-*/tiresias only.

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md
- plans/v1-ga/TIR-10.md

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/conn-refused
- `docker exec mcp-github curl https://api.github.com` succeeds

## Verification approach
Run the above Docker exec commands on the appliance and confirm the expected outcomes.

## Risks
- Potential DNS resolution issues within the internal network causing service disruptions.
- Misconfiguration may inadvertently block legitimate egress traffic.
