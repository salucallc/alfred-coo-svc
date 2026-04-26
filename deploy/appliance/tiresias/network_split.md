# Network Split Documentation

This document describes the split of Docker networks for the appliance.

- **mc-internal**: No internet route; services: alfred-coo, alfred-portal, open-webui, soul-svc.
- **mc-egress**: Allows outbound traffic for MCP services (github, slack, linear, notion, llm) and caddy.

Configuration changes to Docker Compose will place `alfred-coo-svc` and related services on `mc-internal`, while MCP services attach to `mc-egress`.

The split ensures that internal services cannot directly reach external APIs, enforcing egress via the Tiresias proxy.
