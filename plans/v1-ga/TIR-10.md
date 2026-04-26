# TIR-10: Split docker networks

## Target paths
- deploy/appliance/tiresias/network_split.md

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails
- `docker exec mcp-github curl https://api.github.com` succeeds

## Verification approach
Run the above docker exec commands; expect the first to fail (connection refused/DNS) and the second to succeed (200 OK).

## Risks
- Risk R1: Internal network may break DNS; fallback iptables OUTPUT REJECT may be needed (see SAL-09).