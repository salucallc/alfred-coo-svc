# TIR-10: Split docker networks

## Target paths
- deploy/appliance/docker-compose.yml

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails
- `docker exec mcp-github curl --max-time 5 https://api.github.com` succeeds

## Verification approach
Run the above Docker exec commands in the respective containers. The first should error (no DNS/connection), the second should return a successful HTTP response (200).

## Risks
- R1: Internal network DNS breakage may prevent service discovery. Mitigation: fallback iptables OUTPUT REJECT rule in init container.
- R2: Misconfiguration could expose internal services. Mitigation: CI test ensures correct network attachment.
