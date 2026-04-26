# TIR-10: Split docker networks

## Target paths
- deploy/appliance/docker-compose.yml
- deploy/appliance/tiresias/network_split.md

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/conn-refused
- `docker exec mcp-github curl https://api.github.com` succeeds

## Verification approach
- Execute the above docker commands in a CI test environment and verify the expected outcomes.

## Risks
- Potential disruption of internal service communication if network assignments are incorrect.
- DNS resolution may fail for internal services; fallback `iptables OUTPUT REJECT` rule in init container mitigates this.
