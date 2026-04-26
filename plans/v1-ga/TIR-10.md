# TIR-10: Split Docker Networks

## Target paths
- `deploy/appliance/docker-compose.yml`
- `deploy/appliance/tiresias/network_split.md`

## Acceptance criteria
- `docker exec alfred-coo curl --max-time 5 https://api.github.com` fails
- `docker exec mcp-github curl https://api.github.com` succeeds

## Verification approach
- Run the above `docker exec` commands after deploying the compose stack and confirm the expected outcomes.

## Risks
- Misconfiguration could unintentionally block required egress for services; verify each service's network assignment.
