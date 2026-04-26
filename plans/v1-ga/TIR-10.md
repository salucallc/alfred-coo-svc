# TIR-10: Split docker networks

## Target paths
- deploy/appliance/docker-compose.yml

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails with DNS/conn-refused; `docker exec mcp-github curl` same URL succeeds

## Verification approach
Run the two docker exec commands and assert the first times out or returns a DNS/connect error, while the second returns HTTP 200.

## Risks
- Risk R1 (plan A §6): if internal:true breaks DNS, fallback to iptables OUTPUT REJECT in init container; depends on TIR-09.