# TIR-10: Split docker networks

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/connection refused.
- `docker exec mcp-github curl --max-time 5 https://api.github.com` succeeds.

## Verification approach
- Execute the above Docker commands in the respective containers and assert the expected outcomes.

## Risks
- R1 (plan A §6): if `internal:true` breaks DNS, fallback to iptables OUTPUT REJECT in init container; depends on TIR-09.