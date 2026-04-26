# Network Split for Docker Compose (TIR-10)

## Purpose

Separate internal service traffic (`mc-internal`) from egress‑only traffic (`mc-egress`).

- `alfred-coo-svc` runs on `mc-egress` and can only reach services attached to that network.
- All MCP services (`mcp-github`, `mcp-slack`, `mcp-linear`, `mcp-notion`) run on `mc-internal` and are reachable via the `tiresias-proxy`.

## Implementation

- Added two new bridge networks: `mc-internal` and `mc-egress`.
- Updated `alfred-coo-svc` to use `mc-egress`.
- Updated all MCP services to use `mc-internal`.
- Kept existing `appliance` network for external access via Caddy.

## Verification

Run the following commands inside the container:

```bash
# COO container should fail direct GitHub access
docker exec alfred-coo curl --max-time 5 https://api.github.com || echo "Expected failure"

# MCP‑GitHub container should succeed
docker exec mcp-github curl https://api.github.com || echo "Unexpected failure"
```

## Risks

- Misconfiguration could break internal service communication.
- Ensure Docker‑compose version supports multiple networks per service.
