# OPS-07: Agent sidecars for tiresias/portal/mcp-core

## Target paths
- `deploy/appliance/infisical/agent_sidecar.yml`

## Acceptance criteria
- Same for each service

## Verification approach
- Verify that the sidecar service starts with `docker compose up` and mounts `/run/secrets` correctly.
- Confirm that `cat /run/secrets/<svc>_<key>` matches the value in the Infisical UI for each service.

## Risks
- Ensure no existing services are disrupted; sidecar runs in its own container on the `mc-ops` network.
