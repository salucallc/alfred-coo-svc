# Network Split Documentation

This document describes the split Docker networks for the appliance:

- **mc-internal**: Internal bridge network for services that should not have internet access. Includes `alfred-coo`, `portal`, `open-webui`.
- **mc-egress**: Bridge network that connects to external services via `mcp-*` containers. Used by `caddy` and `mcp-github`.

The `docker-compose.yml` is configured to attach containers to the appropriate network accordingly.
