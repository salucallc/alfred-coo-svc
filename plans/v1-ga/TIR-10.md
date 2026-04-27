# TIR-10: Split Docker Networks

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md

## Acceptance criteria
- APE/V: `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/connection refusal; `docker exec mcp-github curl --max-time 5 https://api.github.com` succeeds.

## Verification approach
- Execute the above curl commands inside the respective containers and confirm the expected success/failure outcomes.
- Run the CI smoke test `docker compose up` and ensure no services report connectivity errors.

## Risks
- **R1** (plan A §6): If `internal:true` breaks DNS, the fallback iptables `OUTPUT REJECT` rule in the init container may block legitimate internal traffic.
