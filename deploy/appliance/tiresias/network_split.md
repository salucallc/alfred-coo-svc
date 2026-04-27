# Network Split (TIR-10)

This document describes the Docker network topology split introduced in TIR-10.

- **mc-internal**: bridge network for internal services (coo, portal, open-webui, soul) with `internal: true` – no direct internet access.
- **mc-egress**: bridge network for egress services (caddy, mcp-*, tiresias) that retain outbound connectivity.

The split enforces that `alfred-coo` cannot directly reach external APIs; all egress is routed through the `tiresias-proxy` container on the `mc-egress` network.

## Implementation notes
- Updated `docker-compose.yml` to attach services to the appropriate network.
- Added fallback iptables rule in the init container to reject DNS queries if internal routing fails (see Risk R1).
